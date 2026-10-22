"""数据模型：菜单、优惠券、配送规则、方案。

金额一律使用「分」(int) 参与计算，只在展示层转成元，避免浮点误差。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

# 餐品大类。与麦当劳 MCP query-meals 返回的分类做映射后归一到这几个桶。
STAPLE = "staple"    # 主食：汉堡 / 卷 / 饭
DRINK = "drink"      # 饮料
SIDE = "side"        # 小食：薯条 / 鸡块 / 派
DESSERT = "dessert"  # 甜品：冰淇淋 / 麦旋风
OTHER = "other"

CATEGORY_LABELS = {
    STAPLE: "主食",
    DRINK: "饮料",
    SIDE: "小食",
    DESSERT: "甜品",
    OTHER: "其他",
}


def yuan(cents: int) -> str:
    """分 -> 元，保留两位小数，整数时不带小数。"""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    if cents % 100 == 0:
        return f"{sign}{cents // 100}"
    return f"{sign}{cents // 100}.{cents % 100:02d}"


@dataclass(frozen=True)
class MenuItem:
    """一个可售餐品（SKU）。"""

    code: str
    name: str
    price_cents: int
    category: str = OTHER
    kcal: int | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.price_cents < 0:
            raise ValueError(f"价格不能为负: {self.name}")

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, "其他")


@dataclass(frozen=True)
class Coupon:
    """一张券。

    threshold_cents: 使用门槛（订单小计下限）
    discount_cents:  减免金额
    scope:           限定品类，all 表示全场通用
    """

    coupon_id: str
    name: str
    threshold_cents: int
    discount_cents: int
    scope: str = "all"
    note: str = ""

    def applies(self, subtotal_cents: int, scope_subtotal_cents: int | None = None) -> bool:
        if subtotal_cents < self.threshold_cents:
            return False
        if self.scope == "all":
            return True
        return (scope_subtotal_cents or 0) >= self.threshold_cents


@dataclass(frozen=True)
class DeliveryRule:
    """配送费规则：小计达到 free_threshold_cents 即免配送费，否则收 fee_cents。"""

    fee_cents: int = 900
    free_threshold_cents: int = 3000

    def fee_for(self, subtotal_cents: int) -> int:
        return 0 if subtotal_cents >= self.free_threshold_cents else self.fee_cents

    def gap(self, subtotal_cents: int) -> int:
        """距离免配送费还差多少分，已满足则为 0。"""
        return max(0, self.free_threshold_cents - subtotal_cents)


@dataclass
class Plan:
    """一个完整的点单方案。"""

    label: str
    items: list[tuple[MenuItem, int]] = field(default_factory=list)  # (餐品, 份数)
    subtotal_cents: int = 0
    discount_cents: int = 0
    delivery_cents: int = 0
    coupon: Coupon | None = None
    note: str = ""

    @property
    def pay_cents(self) -> int:
        return self.subtotal_cents - self.discount_cents + self.delivery_cents

    @property
    def kcal(self) -> int:
        return sum((it.kcal or 0) * q for it, q in self.items)

    @property
    def has_unknown_kcal(self) -> bool:
        return any(it.kcal is None for it, _ in self.items)

    def summary(self) -> str:
        parts = []
        for it, q in self.items:
            parts.append(it.name if q == 1 else f"{it.name}×{q}")
        return " + ".join(parts) if parts else "（空）"


def total_kcal(items: Iterable[tuple[MenuItem, int]]) -> int:
    return sum((it.kcal or 0) * q for it, q in items)
