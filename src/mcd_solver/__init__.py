"""麦麦凑单最优解 —— 基于麦当劳 MCP 的凑单精确求解器。"""

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
