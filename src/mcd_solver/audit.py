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


@dataclass
class SlotStat:
    """一个套餐的「配菜位」定价。

    配菜位 = 套餐价 − 主食（第一个 round 的默认选项）单点价。
    它把套餐拆成两段：主食你本来就买，配菜是套餐额外绑给你的。
    如果配菜位等于配菜的单点合计，说明这个「套餐」只是打包，不是折扣。
    """

    name: str
    combo_cents: int
    main_name: str
    main_cents: int
    slot_cents: int
    side_cents: int
    side_desc: str

    @property
    def is_flat(self) -> bool:
        """配菜位没有让价（分毫不差）。"""
        return self.slot_cents == self.side_cents

    @property
    def give_cents(self) -> int:
        """配菜位相对单点合计让了多少钱，正数才是真优惠。"""
        return self.side_cents - self.slot_cents


def slot_pricing(audits: Sequence[ComboAudit]) -> list[SlotStat]:
    """把可比套餐换算成「主食 + 配菜位」两段定价，用于识别打包与折扣。"""
    out: list[SlotStat] = []
    for a in audits:
        if not a.comparable or len(a.parts) < 2:
            continue
        main_name, main_cents = a.parts[0]
        if main_cents is None:
            continue
        side = a.parts[1:]
        if any(p is None for _, p in side):
            continue
        side_cents = sum(p for _, p in side if p is not None)
        out.append(
            SlotStat(
                name=a.combo.name,
                combo_cents=a.combo.price_cents,
                main_name=main_name,
                main_cents=main_cents,
                slot_cents=a.combo.price_cents - main_cents,
                side_cents=side_cents,
                side_desc=" + ".join(f"{n} {yuan(p or 0)}" for n, p in side),
            )
        )
    return out


def explain_slots(stats: Sequence[SlotStat]) -> str:
    """把配菜位定价的分布讲清楚：多数套餐的配菜位落在同一个价格锚点上。"""
    if not stats:
        return "没有可比的套餐。"

    from collections import Counter

    dist = Counter(s.slot_cents for s in stats)
    anchor, hits = dist.most_common(1)[0]
    flat = [s for s in stats if s.is_flat]
    over = [s for s in stats if s.slot_cents > s.side_cents]
    out = [
        f"把 {len(stats)} 个可比套餐拆成「主食 + 配菜位」后：",
        "",
        f"· {hits} 个套餐的配菜位定价 = {yuan(anchor)} 元，占 {hits / len(stats) * 100:.0f}%",
        f"· {len(flat)} 个套餐的配菜位与配菜单点合计完全相等（纯打包，零让价）",
    ]
    if over:
        out.append(f"· {len(over)} 个套餐的配菜位甚至高于配菜单点合计")
    best = max(stats, key=lambda s: s.give_cents)
    if best.give_cents > 0:
        out.append("")
        out.append(
            f"让价最多的「{best.name}」：配菜位 {yuan(best.slot_cents)} 元，"
            f"配菜单点合计 {yuan(best.side_cents)} 元，让了 {yuan(best.give_cents)} 元"
        )
    return "\n".join(out)


def render_slots(stats: Sequence[SlotStat]) -> str:
    rows = sorted(stats, key=lambda s: s.slot_cents)
    lines = [
        f"{'套餐价':>8}  {'主食单价':>8}  {'配菜位':>7}  {'配菜单点':>8}  {'让价':>7}  套餐",
        "-" * 76,
    ]
    for s in rows:
        give = f"{s.give_cents / 100:+.2f}" if s.give_cents else "0.00"
        lines.append(
            f"{yuan(s.combo_cents):>8}  {yuan(s.main_cents):>8}  {yuan(s.slot_cents):>7}"
            f"  {yuan(s.side_cents):>8}  {give:>7}  {s.name}"
        )
    return "\n".join(lines)


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
