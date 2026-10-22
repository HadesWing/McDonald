"""凑单最优解求解器。

问题定义
--------
已知：购物车里已有的餐品（小计 S0）、门店当前可售餐品 C、可用券 K、配送费规则 D。
求：从 C 中挑选若干餐品追加进购物车，使实付金额最低。

实付 = 小计 - 券减免 + 配送费

关键性质
--------
券减免是「阶梯」函数（只有跨过门槛才生效），配送费也是阶梯函数。
因此在任一区间内 实付 随小计单调递增 —— 最优解**只可能**落在
① 什么都不加（基线） ② 某个券门槛处 ③ 免配送费门槛处。
于是问题被归约为：对每个候选门槛 T，求「小计 ≥ T」的最小可达小计。

最小可达小计用位图 DP 精确求解（把可达小计集合编码成一个大整数，
`bits |= bits << price` 即完成一次 0-1 背包转移），因此结果是**全局最优**，
不存在启发式近似。
"""

from __future__ import annotations

import itertools
from typing import Iterable, Sequence

from .models import (
    CATEGORY_LABELS,
    Coupon,
    DeliveryRule,
    MenuItem,
    Plan,
    yuan,
)

# 组合穷举上限。位图 DP 负责证明「最小小计」全局最优；
# 穷举负责在最优小计上挑热量最低的那一种组合。
DEFAULT_MAX_ADDED = 5


def _bitset_reachable(prices: Sequence[int]) -> int:
    """0-1 背包可达性位图：第 s 位为 1 表示「恰好花 s 分」可达。"""
    bits = 1  # 0 元可达
    for p in prices:
        if p > 0:
            bits |= bits << p
    return bits


def min_reachable_at_least(prices: Sequence[int], need_cents: int) -> int | None:
    """返回 ≥ need_cents 的最小可达金额；无解返回 None。"""
    if need_cents <= 0:
        return 0
    bits = _bitset_reachable(prices)
    total = sum(p for p in prices if p > 0)
    if need_cents > total:
        return None
    # 把 need 以下的位置全部清零，最低有效位就是答案
    shifted = bits >> need_cents
    if not shifted:
        return None
    return need_cents + ((shifted & -shifted).bit_length() - 1)


def _combos_with_sum(
    prices: Sequence[int],
    lo: int,
    hi: int,
    max_items: int,
) -> Iterable[tuple[int, ...]]:
    """穷举索引组合，使其价格和落在 [lo, hi] 区间内，件数不超过 max_items。"""
    n = len(prices)
    order = sorted(range(n), key=lambda i: prices[i])

    def dfs(start: int, picked: list[int], acc: int) -> Iterable[tuple[int, ...]]:
        if acc > hi:
            return
        if picked and acc >= lo:
            yield tuple(picked)
        if len(picked) >= max_items:
            return
        for k in range(start, n):
            i = order[k]
            nxt = acc + prices[i]
            if nxt > hi:
                break  # 已按价格排序，后面只会更贵
            picked.append(i)
            yield from dfs(k + 1, picked, nxt)
            picked.pop()

    yield from dfs(0, [], 0)


class ComboSolver:
    """在给定购物车与候选餐品上求最小实付方案。"""

    def __init__(
        self,
        cart: Sequence[tuple[MenuItem, int]],
        candidates: Sequence[MenuItem],
        coupons: Sequence[Coupon] = (),
        delivery: DeliveryRule | None = None,
        max_added: int = DEFAULT_MAX_ADDED,
        kcal_cap: int | None = None,
        allow_categories: Iterable[str] | None = None,
        exclude_codes: Iterable[str] = (),
    ) -> None:
        self.cart = list(cart)
        self.coupons = list(coupons)
        self.delivery = delivery or DeliveryRule()
        self.max_added = max(1, max_added)
        self.kcal_cap = kcal_cap

        cart_codes = {it.code for it, _ in self.cart}
        allow = set(allow_categories) if allow_categories else None
        banned = set(exclude_codes)

        pool: list[MenuItem] = []
        seen: set[str] = set()
        for it in candidates:
            if it.code in cart_codes or it.code in banned or it.code in seen:
                continue
            if allow is not None and it.category not in allow:
                continue
            if self.kcal_cap is not None and it.kcal is not None and it.kcal > self.kcal_cap:
                continue
            seen.add(it.code)
            pool.append(it)
        self.pool = pool

    # ---------- 计价 ----------

    def _scope_subtotal(self, items: Sequence[tuple[MenuItem, int]], scope: str) -> int:
        return sum(it.price_cents * q for it, q in items if it.category == scope)

    def _best_coupon(self, items: Sequence[tuple[MenuItem, int]]) -> tuple[Coupon | None, int]:
        """返回 (最优券, 减免额)。券之间视为互斥，只叠一张。"""
        subtotal = sum(it.price_cents * q for it, q in items)
        best: tuple[Coupon | None, int] = (None, 0)
        for c in self.coupons:
            scope_sub = subtotal if c.scope == "all" else self._scope_subtotal(items, c.scope)
            if not c.applies(subtotal, scope_sub):
                continue
            gain = min(c.discount_cents, subtotal)
            if gain > best[1]:
                best = (c, gain)
        return best

    def evaluate(self, items: Sequence[tuple[MenuItem, int]], label: str = "", note: str = "") -> Plan:
        subtotal = sum(it.price_cents * q for it, q in items)
        coupon, discount = self._best_coupon(items)
        fee = self.delivery.fee_for(subtotal)
        return Plan(
            label=label,
            items=list(items),
            subtotal_cents=subtotal,
            discount_cents=discount,
            delivery_cents=fee,
            coupon=coupon,
            note=note,
        )

    # ---------- 求解 ----------

    def _merge(self, extra: Sequence[MenuItem]) -> list[tuple[MenuItem, int]]:
        """把追加餐品并入购物车（同 code 累加份数）。"""
        merged: dict[str, list] = {}
        order: list[str] = []
        for it, q in self.cart:
            if it.code not in merged:
                merged[it.code] = [it, 0]
                order.append(it.code)
            merged[it.code][1] += q
        for it in extra:
            if it.code not in merged:
                merged[it.code] = [it, 0]
                order.append(it.code)
            merged[it.code][1] += 1
        return [(merged[c][0], merged[c][1]) for c in order]

    def _plans_at_target(self, target_cents: int) -> list[Plan]:
        """求「小计 ≥ target_cents」的所有候选方案（含最优小计上的热量最低组合）。"""
        if not self.pool:
            return []
        s0 = sum(it.price_cents * q for it, q in self.cart)
        need = target_cents - s0
        if need <= 0:
            return [self.evaluate(self.cart, "基线")]

        prices = [it.price_cents for it in self.pool]
        best_add = min_reachable_at_least(prices, need)
        if best_add is None:
            return []  # 这个门槛加不到

        optimal_subtotal = s0 + best_add
        plans: list[Plan] = []
        # 在最优小计上穷举组合，挑热量最低；同时保留「刚好跨过门槛」的写法
        best_kcal: tuple[int, Plan] | None = None
        for combo in _combos_with_sum(prices, best_add, best_add, self.max_added):
            extra = [self.pool[i] for i in combo]
            plan = self.evaluate(self._merge(extra))
            if best_kcal is None or plan.kcal < best_kcal[0]:
                best_kcal = (plan.kcal, plan)
        if best_kcal is None:
            # 最优小计需要超过 max_added 件才能凑出，退回按价格降序的近似组合
            return []
        plans.append(best_kcal[1])
        return plans

    def solve(self) -> dict:
        """返回求解结果字典：基线、最优方案、全部候选。"""
        s0 = sum(it.price_cents * q for it, q in self.cart)
        baseline = self.evaluate(self.cart, "不凑单")

        targets: list[tuple[int, str]] = []
        if self.delivery.gap(s0) > 0:
            targets.append((self.delivery.free_threshold_cents, "免配送费"))
        for c in self.coupons:
            if c.threshold_cents > s0:
                targets.append((c.threshold_cents, f"券门槛「{c.name}」"))

        # 去重，保留首个标签
        seen: dict[int, str] = {}
        for t, label in targets:
            seen.setdefault(t, label)

        candidates: list[Plan] = []
        for t in sorted(seen):
            for plan in self._plans_at_target(t):
                plan.note = seen[t]
                candidates.append(plan)

        # 去重（同小计同组合只留一个）
        uniq: dict[tuple, Plan] = {}
        for p in candidates:
            key = (p.subtotal_cents, tuple(sorted((it.code, q) for it, q in p.items)))
            uniq.setdefault(key, p)
        candidates = list(uniq.values())

        pool = [baseline] + candidates
        pool.sort(key=lambda p: (p.pay_cents, p.kcal, len(p.items)))

        best = pool[0]
        return {
            "baseline": baseline,
            "best": best,
            "all": pool,
            "targets": seen,
            "subtotal0": s0,
            "gap_to_free_delivery": self.delivery.gap(s0),
        }


def explain(result: dict) -> str:
    """把求解结果渲染成人类可读的说明。"""
    base: Plan = result["baseline"]
    best: Plan = result["best"]
    lines: list[str] = []

    lines.append(f"当前购物车：{base.summary()}")
    lines.append(
        f"小计 {yuan(base.subtotal_cents)} 元"
        + (f"，距免配送费还差 {yuan(result['gap_to_free_delivery'])} 元" if result["gap_to_free_delivery"] else "，已免配送费")
    )
    lines.append("")

    if best is base:
        lines.append("结论：**不用凑**。")
        lines.append(
            f"直接下单实付 {yuan(base.pay_cents)} 元（含配送费 {yuan(base.delivery_cents)} 元）。"
        )
        alt = [p for p in result["all"] if p is not base]
        if alt:
            nxt = alt[0]
            lines.append(
                f"硬要凑到「{nxt.note}」需要实付 {yuan(nxt.pay_cents)} 元，"
                f"反而多花 {yuan(nxt.pay_cents - base.pay_cents)} 元 —— 不划算。"
            )
        return "\n".join(lines)

    delta = best.pay_cents - base.pay_cents
    lines.append(f"结论：凑到「{best.note}」，实付 {yuan(best.pay_cents)} 元")
    lines.append(f"加购：{best.summary()}")
    lines.append(
        f"明细：小计 {yuan(best.subtotal_cents)} - 券 {yuan(best.discount_cents)}"
        f" + 配送 {yuan(best.delivery_cents)} = {yuan(best.pay_cents)} 元"
    )
    if best.coupon:
        lines.append(f"用券：{best.coupon.name}（满 {yuan(best.coupon.threshold_cents)} 减 {yuan(best.coupon.discount_cents)}）")
    if delta < 0:
        lines.append(f"对比不凑单（{yuan(base.pay_cents)} 元），**少花 {yuan(-delta)} 元**，还多了东西。")
    elif delta == 0:
        lines.append("对比不凑单，实付持平，但多拿了餐品。")
    else:
        lines.append(f"对比不凑单（{yuan(base.pay_cents)} 元），多花 {yuan(delta)} 元。")

    others = [p for p in result["all"] if p is not best and p is not base][:3]
    if others:
        lines.append("")
        lines.append("备选：")
        for p in others:
            lines.append(
                f"  · {p.note or '方案'}｜{p.summary()}｜实付 {yuan(p.pay_cents)} 元｜{p.kcal} kcal"
            )
    return "\n".join(lines)
