"""菜单快照的读写。

快照的来源有两条：
1. 由 `mcd-mcp` 实时拉取（见 mcp_client.py），落盘为 JSON，供离线演示与基准测试复用；
2. 仓库内置的 `data/sample_menu.json`，用于没有 Token 时也能跑通全流程。

快照格式刻意保持扁平，方便人肉阅读与 diff：

{
  "meta": {"store": "...", "fetched_at": "...", "source": "mcd-mcp"},
  "delivery": {"fee_cents": 900, "free_threshold_cents": 3000},
  "items": [{"code": "...", "name": "...", "price_cents": 2450, "category": "staple", "kcal": 590}],
  "coupons": [{"coupon_id": "...", "name": "...", "threshold_cents": 4000, "discount_cents": 1000}]
}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import DESSERT, DRINK, OTHER, SIDE, STAPLE, Coupon, DeliveryRule, MenuItem

# 麦当劳 MCP 返回的分类名 -> 内部大类
_CATEGORY_ALIASES = {
    "汉堡": STAPLE,
    "汉堡包": STAPLE,
    "主食": STAPLE,
    "卷": STAPLE,
    "饭": STAPLE,
    "饮料": DRINK,
    "饮品": DRINK,
    "咖啡": DRINK,
    "小食": SIDE,
    "配餐": SIDE,
    "薯条": SIDE,
    "甜品": DESSERT,
    "冰淇淋": DESSERT,
    "冰品": DESSERT,
    "dessert": DESSERT,
    "drink": DRINK,
    "side": SIDE,
    "staple": STAPLE,
}


def normalize_category(raw: str | None) -> str:
    if not raw:
        return OTHER
    key = str(raw).strip()
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    for alias, cat in _CATEGORY_ALIASES.items():
        if alias in key:
            return cat
    return OTHER


def _yuan_to_cents(value: Any) -> int:
    """兼容 24.5 / \"24.5\" / \"¥24.50\" 等写法。"""
    if isinstance(value, (int, float)):
        return int(round(float(value) * 100))
    text = str(value).strip().replace("¥", "").replace("￥", "").replace("元", "")
    return int(round(float(text) * 100))


def load_snapshot(path: str | Path) -> dict:
    """读取快照并转成运行期对象。"""
    return parse_snapshot(json.loads(Path(path).read_text(encoding="utf-8")))


def parse_snapshot(data: dict) -> dict:
    """把快照 dict（磁盘格式或 MCP 实时抓取结果）转成运行期对象。"""
    items: list[MenuItem] = []
    for raw in data.get("items", []):
        items.append(
            MenuItem(
                code=str(raw["code"]),
                name=raw["name"],
                price_cents=raw.get("price_cents") or _yuan_to_cents(raw.get("price", 0)),
                category=normalize_category(raw.get("category")),
                kcal=raw.get("kcal"),
                tags=tuple(raw.get("tags", ())),
            )
        )

    coupons = [
        Coupon(
            coupon_id=str(c.get("coupon_id", c.get("id", ""))),
            name=c.get("name", "优惠券"),
            threshold_cents=c.get("threshold_cents") or _yuan_to_cents(c.get("threshold", 0)),
            discount_cents=c.get("discount_cents") or _yuan_to_cents(c.get("discount", 0)),
            scope=c.get("scope", "all"),
            note=c.get("note", ""),
        )
        for c in data.get("coupons", [])
    ]

    d = data.get("delivery", {})
    delivery = DeliveryRule(
        fee_cents=d.get("fee_cents", 900),
        free_threshold_cents=d.get("free_threshold_cents", 3000),
    )

    return {
        "meta": data.get("meta", {}),
        "items": items,
        "combos": data.get("combos", []),
        "coupons": coupons,
        "delivery": delivery,
        "by_code": {it.code: it for it in items},
        "by_name": {it.name: it for it in items},
    }
