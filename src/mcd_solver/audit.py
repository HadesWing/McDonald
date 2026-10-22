"""套餐差价审计：套餐价 vs 单品原价合计，到底有没有折扣。

这个模块回答一个所有人都以为自己知道、但其实没人算过的问题：
**套餐真的比单点便宜吗？**

做法不猜、不估计：
- 套餐价 = 官方 calculate-price 对套餐编码报价
- 单点价 = 官方 calculate-price 对套餐内各商品的单品编码一起报价
两边都是官方返回的实付金额，相减即真实差价。

如果一个套餐的子项在门店无法单点（很多套餐专属饮料就是这样），
该套餐标记为「不可比」，并明确列出缺了哪一项 —— 不拿估算填坑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .decompiler import Combo, Choice, load_combos
from .models import MenuItem, yuan

_SIZE_RE = re.compile(r"(小杯|中杯|大杯|迷你)")
_NOISE_RE = re.compile(r"[\s\u3000“”\"'（）()【】·]")

VERDICT_LABELS = {
    "zero": "零折扣",
    "real": "真有折扣",
    "reverse": "反向溢价",
    "unknown": "不可比",
}


def normalize_name(name: str) -> str:
    """把名字里的杯型、无糖、冰热等修饰去掉，便于跨编码匹配同一商品。"""
    n = _NOISE_RE.sub("", name or "")
    n = n.replace("无糖", "")
    return n


def base_name(name: str) -> str:
    return _SIZE_RE.sub("", normalize_name(name))


@dataclass
class ComboAudit:
    combo: Combo
    parts: list[tuple[str, int | None]] = field(default_factory=list)  # (商品名, 单点价)
    single_cents: int | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def comparable(self) -> bool:
        return self.single_cents is not None and not self.missing

    @property
    def diff_cents(self) -> int | None:
        """套餐价 - 单点合计。负数是套餐更便宜。"""
        if not self.comparable:
            return None
        return self.combo.price_cents - (self.single_cents or 0)

    @property
    def discount(self) -> float | None:
        if not self.comparable or not self.single_cents:
            return None
        return self.combo.price_cents / self.single_cents

    @property
    def verdict(self) -> str:
        d = self.diff_cents
        if d is None:
            return "unknown"
        if d == 0:
            return "zero"
        return "real" if d < 0 else "reverse"

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABELS[self.verdict]

    def detail(self) -> str:
        parts = " + ".join(f"{n}{'' if p is None else ' ' + yuan(p)}" for n, p in self.parts)
        return parts


def resolve_component(
    pool: Sequence[MenuItem],
    name: str,
    code: str,
    exclude_codes: set[str] | None = None,
) -> MenuItem | None:
    """把套餐子项映射到门店可单点的商品。

    依次尝试：编码精确命中 → 名字精确命中 → 去掉杯型/修饰后基础名命中。
    exclude_codes 用来排除套餐自身与其它套餐，避免「套餐被当成自己的子项」这种自指。
    """
    exclude_codes = exclude_codes or set()

    def usable(it: MenuItem) -> bool:
        return it.price_cents > 0 and it.code not in exclude_codes

    by_code = {it.code: it for it in pool}
    if code in by_code and usable(by_code[code]):
        return by_code[code]

    by_name = {it.name: it for it in pool}
    if name in by_name and usable(by_name[name]):
        return by_name[name]

    target = base_name(name)
    cands = [
        it
        for it in pool
        if usable(it) and (base_name(it.name) == target or it.name == normalize_name(name))
    ]
    if cands:
        return cands[0]

    # 最后退一步：名字互相包含（如「麦乐鸡5块」vs「麦乐鸡 5 块」）
    loose = [
        it
        for it in pool
        if usable(it) and target and (target in base_name(it.name) or base_name(it.name) in target)
    ]
    if len(loose) == 1:
        return loose[0]
    return None


_PIECE_RE = re.compile(r"（\s*(\d+)\s*件|\(\s*(\d+)\s*件|(\d+)\s*件任选")


def declared_pieces(combo: Combo) -> int | None:
    """从套餐名里读「N 件任选」这类件数声明。"""
    m = _PIECE_RE.search(combo.name or "")
    if not m:
        return None
    return int(next(g for g in m.groups() if g))


def audit_combo(combo: Combo, pool: Sequence[MenuItem], exclude_codes: set[str] | None = None) -> ComboAudit:
    audit = ComboAudit(combo=combo)
    exclude = set(exclude_codes or set())
    exclude.add(combo.code)

    # 件数声明和实际 round 数量对不上（如「7 件任选」只暴露 3 个 round），
    # 说明接口没有把可选项全部展开，这种套餐不能拿来比价。
    declared = declared_pieces(combo)
    actual = sum(max(1, r.max_qty) for r in combo.rounds)
    if declared and declared > actual:
        audit.missing.append(f"接口只展开 {actual} 项，套餐声明 {declared} 件")
        for r in combo.rounds:
            ch = r.default
            audit.parts.append(((ch.name if ch else r.name), None))
        return audit

    # 有子项可选多份的（如「7 件任选」），默认配置只取一份，
    # 和套餐实际内容对不上，不能拿去比价。
    multi = [r for r in combo.rounds if r.max_qty > 1]
    if multi:
        audit.missing.append("多选套餐：子项可选多份，无法与单点一一对应")
        for r in combo.rounds:
            ch = r.default
            audit.parts.append(((ch.name if ch else r.name), None))
        return audit

    total = 0
    for r in combo.rounds:
        ch: Choice | None = r.default
        if ch is None:
            continue
        hit = resolve_component(pool, ch.name, ch.code, exclude)
        if hit is None:
            audit.missing.append(ch.name)
            audit.parts.append((ch.name, None))
        else:
            audit.parts.append((hit.name, hit.price_cents))
            total += hit.price_cents
    audit.single_cents = None if audit.missing else total
    return audit


def audit_all(combos: Iterable[Combo], pool: Sequence[MenuItem]) -> list[ComboAudit]:
    """审计所有多子项的套餐（单子项的「套餐」其实就是单品，跳过）。"""
    combo_list = list(combos)
    # 只有多子项的才是真套餐；单子项的「套餐」其实就是单品，不能排除，
    # 否则会把「可乐」这类商品一起屏蔽掉，导致大量套餐不可比。
    combo_codes = {c.code for c in combo_list if len(c.rounds) >= 2}
    out: list[ComboAudit] = []
    for c in combo_list:
        if len(c.rounds) < 2:
            continue
        out.append(audit_combo(c, pool, combo_codes))
    return out


def summarize(audits: Sequence[ComboAudit]) -> dict:
    comparable = [a for a in audits if a.comparable]
    zero = [a for a in comparable if a.verdict == "zero"]
    real = [a for a in comparable if a.verdict == "real"]
    reverse = [a for a in comparable if a.verdict == "reverse"]
    saved = sum(-(a.diff_cents or 0) for a in real)
    return {
        "total": len(audits),
        "comparable": len(comparable),
        "zero": len(zero),
        "real": len(real),
        "reverse": len(reverse),
        "unknown": len(audits) - len(comparable),
        "zero_ratio": (len(zero) / len(comparable)) if comparable else 0.0,
        "total_savings_cents": saved,
        "best": min(real, key=lambda a: a.diff_cents or 0) if real else None,
    }


def render_table(audits: Sequence[ComboAudit], only_comparable: bool = True) -> str:
    rows = [a for a in audits if a.comparable] if only_comparable else list(audits)
    rows.sort(key=lambda a: (a.diff_cents if a.diff_cents is not None else 10**9))
    lines = [
        f"{'套餐价':>8}  {'单点价':>8}  {'差价':>7}  {'折扣':>5}  套餐名",
        "-" * 68,
    ]
    for a in rows:
        d = a.diff_cents
        if d is None:
            lines.append(f"{yuan(a.combo.price_cents):>8}  {'—':>8}  {'—':>7}  {'—':>5}  {a.combo.name}（缺 {len(a.missing)} 项）")
            continue
        disc = f"{a.discount * 10:.1f}折" if a.discount else "—"
        sign = f"{d / 100:+.2f}" if d else "0.00"
        lines.append(
            f"{yuan(a.combo.price_cents):>8}  {yuan(a.single_cents or 0):>8}  {sign:>7}  {disc:>5}  {a.combo.name}"
        )
    return "\n".join(lines)


def explain_audit(audits: Sequence[ComboAudit], store: str = "") -> str:
    s = summarize(audits)
    out: list[str] = []
    if store:
        out.append(f"门店：{store}")
    out.append(
        f"审计了 {s['total']} 个套餐，其中 {s['comparable']} 个可以和单点价逐项对齐"
        + (f"（{s['unknown']} 个因门店缺单点商品无法对齐）" if s["unknown"] else "")
    )
    out.append("")
    out.append(
        f"· {s['zero']} 个套餐的价格与单品原价合计**完全相等**（零折扣），占可比套餐的 {s['zero_ratio'] * 100:.0f}%"
    )
    out.append(f"· {s['real']} 个套餐确实更便宜，合计最大可省 {yuan(s['total_savings_cents'])} 元")
    if s["reverse"]:
        out.append(f"· {s['reverse']} 个套餐反而比单点**更贵**")
    if s["best"]:
        b = s["best"]
        out.append("")
        out.append(
            f"折扣最实在的是「{b.combo.name}」：套餐 {yuan(b.combo.price_cents)} 元，"
            f"单点合计 {yuan(b.single_cents or 0)} 元，省 {yuan(-(b.diff_cents or 0))} 元"
        )
    return "\n".join(out)
