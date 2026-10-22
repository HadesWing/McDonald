"""套餐反编译：把套餐价这个黑盒拆开，用真实单品价还原它的折扣结构。

三个层次：

1. **拆解**：套餐由若干 round（子项）组成，每个 round 有若干可选商品，各自带官方差价。
   套餐价 = 基准价 + 所选各商品的官方差价之和。

2. **还原折扣**：把同一份配置按「单点」重新算一遍（各商品在门店的真实单价之和），
   与套餐价对比，得到这个配置的真实折扣率。折扣不是常数 —— 换一个选项它就变了。

3. **找套利**：如果某选项的「套餐内加价」低于它的「单点差价」，
   那么换它会比单点买更划算。把所有选项按这个净值排序，就是「换哪个最赚」。

以上每一步都可以用官方 calculate-price 带 roundList 复核，不是估算。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .models import yuan


@dataclass(frozen=True)
class Choice:
    code: str
    name: str
    diff_cents: int
    is_default: bool = False
    single_cents: int | None = None  # 该商品在门店的单点价，None 表示门店菜单里查不到
    kcal: int | None = None

    @property
    def diff_label(self) -> str:
        return "含" if self.diff_cents == 0 else f"+{yuan(self.diff_cents)}"


@dataclass(frozen=True)
class Round:
    id: int
    name: str
    choices: tuple[Choice, ...] = ()
    min_qty: int = 1
    max_qty: int = 1

    @property
    def default(self) -> Choice | None:
        for c in self.choices:
            if c.is_default:
                return c
        return self.choices[0] if self.choices else None

    @property
    def selectable(self) -> bool:
        return len(self.choices) > 1


@dataclass
class Combo:
    code: str
    name: str
    price_cents: int
    rounds: tuple[Round, ...] = ()
    image: str | None = None

    @property
    def configurable_rounds(self) -> tuple[Round, ...]:
        return tuple(r for r in self.rounds if r.selectable)

    @property
    def config_count(self) -> int:
        n = 1
        for r in self.configurable_rounds:
            n *= max(1, len(r.choices))
        return n


@dataclass
class Config:
    """一种具体的套餐搭法。"""

    combo: Combo
    picks: dict[int, Choice] = field(default_factory=dict)
    price_cents: int = 0
    single_cents: int | None = None
    kcal: int | None = None

    @property
    def savings_cents(self) -> int | None:
        if self.single_cents is None:
            return None
        return self.single_cents - self.price_cents

    @property
    def discount(self) -> float | None:
        """实付 ÷ 单点价。0.85 表示 8.5 折。"""
        if not self.single_cents:
            return None
        return self.price_cents / self.single_cents

    def summary(self) -> str:
        return " + ".join(c.name for _, c in sorted(self.picks.items()))


# ---------- 加载 ----------


def load_combos(snapshot: dict) -> tuple[list[Combo], dict[str, object]]:
    """从快照里构造 Combo 列表，并回填每个选项的单点价与热量。

    snapshot 可以来自 catalog.load_snapshot（items 是 MenuItem），
    也可以直接是原始 JSON（items 是 dict），两种都兼容。
    """
    items = list(snapshot.get("items", []))
    item_by_code: dict[str, object] = {}
    item_by_name: dict[str, object] = {}
    for it in items:
        if isinstance(it, dict):
            item_by_code[str(it.get("code"))] = it
            item_by_name[str(it.get("name"))] = it
        else:
            item_by_code[it.code] = it
            item_by_name[it.name] = it

    def _field(item, key, default=None):
        if item is None:
            return default
        return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)

    combos: list[Combo] = []
    for raw in snapshot.get("combos", []):
        rounds: list[Round] = []
        for r in raw.get("rounds", []):
            choices: list[Choice] = []
            for ch in r.get("choices", []):
                hit = item_by_code.get(str(ch["code"])) or item_by_name.get(ch.get("name"))
                choices.append(
                    Choice(
                        code=str(ch["code"]),
                        name=ch.get("name") or str(ch["code"]),
                        diff_cents=int(ch.get("diff_cents") or 0),
                        is_default=bool(ch.get("is_default")),
                        single_cents=_field(hit, "price_cents"),
                        kcal=_field(hit, "kcal"),
                    )
                )
            rounds.append(
                Round(
                    id=int(r.get("id") or 0),
                    name=r.get("name") or "",
                    choices=tuple(choices),
                    min_qty=int(r.get("min") or 1),
                    max_qty=int(r.get("max") or 1),
                )
            )
        combos.append(
            Combo(
                code=str(raw["code"]),
                name=raw.get("name") or str(raw["code"]),
                price_cents=int(raw.get("price_cents") or 0),
                rounds=tuple(rounds),
                image=raw.get("image"),
            )
        )
    return combos, item_by_code


def find_combo(combos: Sequence[Combo], token: str) -> Combo | None:
    """按名字或编码找套餐，支持模糊匹配。"""
    token = (token or "").strip()
    for c in combos:
        if c.code == token:
            return c
    exact = [c for c in combos if c.name == token]
    if exact:
        return exact[0]
    hits = [c for c in combos if token in c.name]
    return min(hits, key=lambda c: len(c.name)) if hits else None


# ---------- 枚举与计算 ----------


def build_config(combo: Combo, picks: dict[int, Choice]) -> Config:
    price = combo.price_cents + sum(ch.diff_cents for ch in picks.values())
    known = [ch.single_cents for ch in picks.values()]
    single = None if any(s is None for s in known) else sum(known)  # type: ignore[arg-type]
    kcals = [ch.kcal for ch in picks.values()]
    kcal = None if any(k is None for k in kcals) else sum(kcals)  # type: ignore[arg-type]
    return Config(combo=combo, picks=dict(picks), price_cents=price, single_cents=single, kcal=kcal)


def default_config(combo: Combo) -> Config:
    return build_config(combo, {r.id: r.default for r in combo.rounds if r.default})


def enumerate_configs(combo: Combo, limit: int = 20000) -> list[Config]:
    """枚举套餐的全部合法搭法。round 目前都是「必选一项」，按笛卡尔积展开。"""
    rounds = list(combo.rounds)
    if not rounds:
        return [default_config(combo)]
    total = 1
    for r in rounds:
        total *= max(1, len(r.choices))
        if total > limit:
            raise ValueError(f"套餐 {combo.name} 的组合数 {total} 超过上限 {limit}")

    configs: list[Config] = []
    for combo_pick in itertools.product(*(r.choices for r in rounds)):
        picks = {r.id: ch for r, ch in zip(rounds, combo_pick)}
        configs.append(build_config(combo, picks))
    configs.sort(key=lambda c: (c.price_cents, c.summary()))
    return configs


@dataclass
class SwapInsight:
    """某个 round 里，把默认选项换成另一个选项的净收益。"""

    round_name: str
    from_choice: Choice
    to_choice: Choice
    combo_delta: int  # 套餐要多付的钱
    single_delta: int | None  # 单点要多付的钱
    worth_it: bool = False  # 套餐内的加价是否低于它本身的单点差价

    @property
    def net_gain(self) -> int | None:
        """换成它，套餐替你多省了多少分。正数 = 划算。"""
        if self.single_delta is None:
            return None
        return self.single_delta - self.combo_delta


def swap_table(combo: Combo) -> list[SwapInsight]:
    """对每个可配置 round，算出「换成 X 值不值」。"""
    out: list[SwapInsight] = []
    for r in combo.configurable_rounds:
        base = r.default
        if base is None:
            continue
        for ch in r.choices:
            if ch.code == base.code:
                continue
            combo_delta = ch.diff_cents - base.diff_cents
            single_delta = None
            if ch.single_cents is not None and base.single_cents is not None:
                single_delta = ch.single_cents - base.single_cents
            insight = SwapInsight(
                round_name=r.name,
                from_choice=base,
                to_choice=ch,
                combo_delta=combo_delta,
                single_delta=single_delta,
            )
            insight.worth_it = single_delta is not None and single_delta > combo_delta
            out.append(insight)
    out.sort(key=lambda s: (s.net_gain is None, -(s.net_gain or 0)))
    return out


@dataclass
class ComboVerdict:
    combo: Combo
    config_count: int
    base: Config
    best: Config | None  # 折扣最狠的搭法
    worst: Config | None  # 折扣最差的搭法

    @property
    def base_savings(self) -> int | None:
        return self.base.savings_cents


def judge(combo: Combo) -> ComboVerdict:
    """给一个套餐下判断：默认搭法省多少、最好/最差能到什么程度。"""
    configs = enumerate_configs(combo)
    base = default_config(combo)
    priced = [c for c in configs if c.savings_cents is not None]
    if not priced:
        return ComboVerdict(combo, len(configs), base, None, None)
    best = max(priced, key=lambda c: c.savings_cents or 0)
    worst = min(priced, key=lambda c: c.savings_cents or 0)
    return ComboVerdict(combo, len(configs), base, best, worst)


def rank_combos(combos: Iterable[Combo], only_configurable: bool = False) -> list[ComboVerdict]:
    """全店套餐按真实折扣率排序（便宜的先）。"""
    verdicts: list[ComboVerdict] = []
    for c in combos:
        if c.price_cents <= 0:
            continue
        if only_configurable and not c.configurable_rounds:
            continue
        try:
            v = judge(c)
        except ValueError:
            continue
        if v.base.discount is not None:
            verdicts.append(v)
    verdicts.sort(key=lambda v: v.base.discount or 9)
    return verdicts


def pareto_frontier(configs: Sequence[Config]) -> list[Config]:
    """价格 / 热量 双目标下的帕累托前沿（两个维度都更优才保留）。"""
    pool = [c for c in configs if c.kcal is not None]
    pool.sort(key=lambda c: (c.price_cents, c.kcal or 0))
    front: list[Config] = []
    best_kcal = float("inf")
    for c in pool:
        if (c.kcal or 0) < best_kcal:
            front.append(c)
            best_kcal = c.kcal or 0
    return front
