"""套餐刺客（Combo Assassin）—— 基于麦当劳 MCP 的套餐定价审计工具。"""

from .models import (
    CATEGORY_LABELS,
    Coupon,
    DeliveryRule,
    MenuItem,
    Plan,
    yuan,
)
from .optimizer import ComboSolver, explain, min_reachable_at_least

__version__ = "0.1.0"

__all__ = [
    "CATEGORY_LABELS",
    "ComboSolver",
    "Coupon",
    "DeliveryRule",
    "MenuItem",
    "Plan",
    "explain",
    "min_reachable_at_least",
    "yuan",
]
