"""单套餐跨店比价：选中一个套餐，看它在附近几家店各卖多少钱。

和 `compare.py` 的区别：
- `compare.py` 比的是**整份菜单**（要先把每家店的菜单全抓下来，一家 8 秒）；
- 这里只比**一个套餐**，对每家店直接调一次 `calculate-price`，5 家店并行约 1 秒。

到店场景要的就是后者：站在门口挑套餐，不可能等五家店各抓一遍菜单。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

from .mcp_client import MCDMCPClient, MCPError


def price_combo(
    client: MCDMCPClient,
    store_code: str,
    combo_code: str,
    order_type: int = 1,
    be_type: int = 1,
) -> dict:
    """问一家店「这个套餐卖多少钱」。拿不到就如实标不可用，不猜。"""
    try:
        resp = client.call_tool(
            "calculate-price",
            {
                "storeCode": str(store_code),
                "orderType": order_type,
                "beType": be_type,
                "items": [{"productCode": str(combo_code), "quantity": 1}],
            },
        )
    except MCPError as exc:
        return {"storeCode": str(store_code), "price_cents": None, "error": str(exc)[:120]}

    data = resp.get("data") or {}
    price = data.get("price")
    products = data.get("productList") or []
    name = products[0].get("productName") if products else None
    if not price:
        return {
            "storeCode": str(store_code),
            "price_cents": None,
            "error": resp.get("message") or "该店没有这个套餐",
        }
    return {"storeCode": str(store_code), "price_cents": int(price), "name": name, "error": None}


def compare_combo(
    combo_code: str,
    stores: Sequence[dict],
    token: str | None = None,
    workers: int = 6,
    order_type: int = 1,
    be_type: int = 1,
) -> dict:
    """对候选门店逐个询价，返回按价格升序排列的结果。

    `stores` 是 `geo.search_stores()` 的输出（含 storeName / address / 营业状态），
    比价结果会和门店信息合并，前端一次就能渲染完整卡片。
    """
    codes = [str(s["storeCode"]) for s in stores]
    if not codes:
        return {"rows": [], "stats": {"available": 0, "max_diff": 0, "cheapest": None}}

    def work(code: str) -> dict:
        # 每家店用独立会话：MCP 的 session 不保证并发安全
        return price_combo(MCDMCPClient(token), code, combo_code, order_type, be_type)

    with ThreadPoolExecutor(max_workers=min(workers, len(codes))) as pool:
        priced = list(pool.map(work, codes))

    by_code = {p["storeCode"]: p for p in priced}
    rows: list[dict] = []
    for s in stores:
        p = by_code.get(str(s["storeCode"])) or {}
        rows.append(
            {
                "storeCode": str(s["storeCode"]),
                "storeName": s.get("storeName"),
                "address": s.get("address"),
                "businessStartTime": s.get("businessStartTime"),
                "businessEndTime": s.get("businessEndTime"),
                "open_now": s.get("open_now"),
                "closing_soon": s.get("closing_soon"),
                "minutes_left": s.get("minutes_left"),
                "price_cents": p.get("price_cents"),
                "error": p.get("error"),
            }
        )

    ok = [r for r in rows if r["price_cents"]]
    ok.sort(key=lambda r: r["price_cents"])
    lo = ok[0]["price_cents"] if ok else None
    hi = ok[-1]["price_cents"] if ok else None
    for r in rows:
        r["is_cheapest"] = bool(ok and r["price_cents"] == lo)
        r["is_dearest"] = bool(ok and r["price_cents"] == hi and hi != lo)
        r["delta_from_cheapest"] = (r["price_cents"] - lo) if (r["price_cents"] and lo) else None

    return {
        "combo_code": str(combo_code),
        "rows": rows,
        "stats": {
            "available": len(ok),
            "unavailable": len(rows) - len(ok),
            "cheapest": ok[0]["storeName"] if ok else None,
            "cheapest_code": ok[0]["storeCode"] if ok else None,
            "low": lo,
            "high": hi,
            "max_diff": (hi - lo) if (lo is not None and hi is not None) else 0,
        },
    }
