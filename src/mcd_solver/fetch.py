"""从麦当劳 MCP 拉取真实门店数据，落盘成可离线复用的快照。

取数策略（为了少打接口）：
1. query-nearby-stores 拿门店
2. query-meals 拿菜单骨架（只有 code + 分类 + 标签）
3. calculate-price **一次性批量**传全部 code，拿名称与真实售价
4. query-meal-detail 逐个拿套餐结构（rounds / choices / diffPrice）与单品特制项
5. list-nutrition-foods 拿营养表，按名称匹配挂到餐品上

注意：MCP 每 Token 每分钟 600 次，这里主动限速。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from .mcp_client import MCDMCPClient
from .models import DESSERT, DRINK, OTHER, SIDE, STAPLE

ORDER_TYPE_STORE = 1
ORDER_TYPE_DELIVERY = 2
BE_STORE = 1
BE_DELIVERY = 2
BE_DRIVE = 5


def guess_category(name: str, raw_category: str = "") -> str:
    """按餐品名判定大类。菜单里的原始分类（人气热卖 / 蘸酱炸鸡）交叉严重，不如按名字稳。"""
    n = name.replace(" ", "")
    if any(k in n for k in ("汉堡", "堡", "卷", "饭", "麦满分", "吉士", "麦香鸡", "板烧")):
        return STAPLE
    if any(k in n for k in ("可乐", "雪碧", "芬达", "咖啡", "拿铁", "美式", "茶", "汁", "奶铁", "水", "奶茶")):
        return DRINK
    if any(k in n for k in ("圣代", "新地", "麦旋风", "冰淇淋", "圆筒", "甜筒", "派")):
        return DESSERT
    if any(k in n for k in ("薯条", "鸡块", "鸡翅", "薯饼", "玉米", "沙拉", "蘸酱", "鸡排")):
        return SIDE
    return OTHER


def parse_nutrition(raw: Any) -> dict[str, dict]:
    """解析 list-nutrition-foods 返回的表格文本。

    形态：`[160]{productName,nutritionDescription,energyKj,...}:` 后面逐行是逗号分隔的数据。
    """
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if isinstance(raw, dict):
        # MCP 把表格塞在 data 字段里，且是带真实换行的字符串
        inner = raw.get("data")
        if isinstance(inner, str):
            text = inner
    table: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("[") or line.startswith("{"):
            continue
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 4:
            continue
        name = cols[0]
        try:
            kcal = int(float(cols[3]))
        except (ValueError, IndexError):
            continue
        table[_norm(name)] = {
            "kcal": kcal,
            "protein_g": _num(cols[4]) if len(cols) > 4 else None,
            "fat_g": _num(cols[5]) if len(cols) > 5 else None,
            "carb_g": _num(cols[6]) if len(cols) > 6 else None,
            "sodium_mg": _num(cols[7]) if len(cols) > 7 else None,
            "calcium_mg": _num(cols[8]) if len(cols) > 8 else None,
        }
    return table


def _num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm(name: str) -> str:
    return re.sub(r"[\s\u3000·\-—()（）]", "", name or "")


def lookup_nutrition(table: dict[str, dict], name: str) -> dict | None:
    key = _norm(name)
    if key in table:
        return table[key]
    # 套餐名常带"三件套/四件套"等后缀，退一步做包含匹配
    for k, v in table.items():
        if k and (k in key or key in k):
            return v
    return None


def _diff_cents(text: str) -> int:
    """把 '+ ¥2.5' / '+¥0' / '¥3' 解析成分。"""
    if not text:
        return 0
    m = re.search(r"([+-]?)\s*¥?\s*([\d.]+)", str(text).replace(",", ""))
    if not m:
        return 0
    sign = -1 if m.group(1) == "-" else 1
    return sign * int(round(float(m.group(2)) * 100))


def fetch_snapshot(
    city: str,
    keyword: str,
    store_index: int = 0,
    order_type: int = ORDER_TYPE_STORE,
    be_type: int = BE_STORE,
    token: str | None = None,
    pause: float = 0.08,
    workers: int = 8,
    store_code: str | None = None,
    log=print,
) -> dict:
    """拉取一家门店的完整快照。

    workers > 1 时，逐个 SKU 的详情查询和套餐选项补价走线程池并发，
    把「实时抓一家店」从一分钟级压到十几秒——本地 serve 模式要靠这个才可用。

    store_code 用于「到店」场景：用户已经从候选列表里点明了是哪家店，
    这时按编码精确命中，而不是靠 store_index 撞运气。
    """
    client = MCDMCPClient(token)

    stores = client.call_tool(
        "query-nearby-stores", {"beType": be_type, "searchType": 2, "city": city, "keyword": keyword}
    ).get("data") or []
    if not stores:
        raise RuntimeError(f"没找到门店：{city} {keyword}")
    if store_code:
        hit = next((s for s in stores if str(s.get("storeCode")) == str(store_code)), None)
        if hit is None:
            raise RuntimeError(f"门店 {store_code} 不在「{city} {keyword}」的搜索结果里，请换个关键词再试")
        store = hit
    else:
        store = stores[store_index]
    store_code = str(store["storeCode"])
    be_code = store.get("beCode")
    log(f"门店：{store['storeName']}（{store_code}）{store.get('address', '')}")

    scope: dict[str, Any] = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
    if be_code and be_type in (BE_DELIVERY, BE_DRIVE):
        scope["beCode"] = be_code

    menu = client.call_tool("query-meals", scope).get("data") or {}
    codes: list[str] = []
    raw_cat: dict[str, str] = {}
    tags: dict[str, list[str]] = {}
    for cat in menu.get("categories", []):
        cat_name = (cat.get("name") or "").replace("\n", " ").strip()
        for meal in cat.get("meals", []):
            code = str(meal.get("code"))
            if not code:
                continue
            if code not in raw_cat:
                codes.append(code)
                raw_cat[code] = cat_name
            tags.setdefault(code, [])
            for t in meal.get("tags", []):
                if t not in tags[code]:
                    tags[code].append(t)
    log(f"菜单骨架：{len(codes)} 个 SKU")

    # 一次批量计价，拿到名称与真实售价
    price_map: dict[str, dict] = {}
    batch = 40
    for i in range(0, len(codes), batch):
        chunk = codes[i : i + batch]
        resp = client.call_tool(
            "calculate-price",
            {**scope, "items": [{"productCode": c, "quantity": 1} for c in chunk]},
        ).get("data") or {}
        for p in resp.get("productList", []):
            price_map[str(p["productCode"])] = {
                "name": p.get("productName"),
                "price_cents": p.get("subtotal") or p.get("productPrice") or 0,
            }
        time.sleep(pause)
    log(f"计价完成：{len(price_map)} 个 SKU 有价")

    # 逐个取详情：套餐拿 rounds，单品拿特制项。
    # MCP 是 HTTP 的，串行一轮要一分钟；这里按 SKU 并发，实时模式才勉强可用。
    details: dict[str, dict] = {}
    done = 0
    for code, detail, err in _map_parallel(
        codes,
        lambda c, cl: cl.call_tool("query-meal-detail", {**scope, "code": c}).get("data") or {},
        workers=workers,
        token=token,
        pause=pause,
    ):
        if err:
            log(f"  ! 详情失败 {code}: {err[:80]}")
        else:
            details[code] = detail
        done += 1
        if done % 25 == 0:
            log(f"  详情 {done}/{len(codes)}")

    nutrition = parse_nutrition(client.call_tool("list-nutrition-foods", {}))
    log(f"营养表：{len(nutrition)} 条")

    items: list[dict] = []
    combos: list[dict] = []
    for code in codes:
        info = price_map.get(code) or {}
        name = info.get("name") or details.get(code, {}).get("name") or code
        price = info.get("price_cents") or 0
        detail = details.get(code) or {}
        rounds = detail.get("rounds") or []
        nut = lookup_nutrition(nutrition, name)

        base = {
            "code": code,
            "name": name,
            "price_cents": price,
            "category": guess_category(name, raw_cat.get(code, "")),
            "raw_category": raw_cat.get(code, ""),
            "tags": tags.get(code, []),
            "kcal": (nut or {}).get("kcal"),
        }
        if nut:
            base["nutrition"] = nut

        if rounds:
            base["is_combo"] = True
            items.append(base)
            combos.append(_build_combo(code, name, price, rounds, detail))
        else:
            items.append(base)

    # 套餐里的选项（如「中薯条」「可乐中杯」）常常不在菜单骨架里，
    # 但它们本身是可单点的。补一次批量计价，否则「套餐省多少」根本算不出来。
    known_codes = {it["code"] for it in items}
    extra: dict[str, str] = {}
    for cb in combos:
        for r in cb["rounds"]:
            for ch in r["choices"]:
                if ch["code"] not in known_codes:
                    extra[ch["code"]] = ch["name"]
    if extra:
        log(f"套餐选项补充计价：{len(extra)} 个")
        priced = _map_parallel(
            sorted(extra),
            lambda c, cl: cl.call_tool(
                "calculate-price", {**scope, "items": [{"productCode": c, "quantity": 1}]}
            ).get("data")
            or {},
            workers=workers,
            token=token,
            pause=pause,
        )
        for c_code, resp, _err in priced:
            c_name = extra[c_code]
            plist = (resp or {}).get("productList") or []
            if not plist:
                # 该编码在本店不可单点（多见于套餐专属饮料），跳过
                continue
            p = plist[0]
            real_name = p.get("productName") or c_name
            nut = lookup_nutrition(nutrition, real_name)
            items.append(
                {
                    "code": c_code,
                    "name": real_name,
                    "price_cents": p.get("subtotal") or p.get("productPrice") or 0,
                    "category": guess_category(real_name),
                    "raw_category": "套餐选项",
                    "tags": ["套餐选项"],
                    "kcal": (nut or {}).get("kcal"),
                    "nutrition": nut or None,
                    "standalone": True,
                }
            )
        log(f"补充后共 {len(items)} 个 SKU")

    snapshot = {
        "meta": {
            "store": store.get("storeName"),
            "store_code": store_code,
            "address": store.get("address"),
            "city": city,
            "keyword": keyword,
            "order_type": order_type,
            "be_type": be_type,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "source": "mcd-mcp",
            "mcp_endpoint": "https://mcp.mcd.cn",
            "tools": [
                "query-nearby-stores",
                "query-meals",
                "calculate-price",
                "query-meal-detail",
                "list-nutrition-foods",
            ],
        },
        "delivery": {"fee_cents": 0, "free_threshold_cents": 0},
        "items": items,
        "combos": combos,
        "coupons": [],
    }
    log(f"完成：{len(items)} 个 SKU，其中套餐 {len(combos)} 个")
    return snapshot


def _build_combo(code: str, name: str, price: int, rounds: list[dict], detail: dict) -> dict:
    """把 MCP 的 rounds 结构整理成稳定的快照格式。"""
    out_rounds = []
    for r in rounds:
        choices = []
        for ch in r.get("choices", []) or []:
            choices.append(
                {
                    "code": str(ch.get("code")),
                    "name": ch.get("name"),
                    "diff_cents": _diff_cents(ch.get("diffPrice")),
                    "is_default": bool(ch.get("isDefault")),
                    "support_modify": bool(ch.get("supportModify")),
                }
            )
        out_rounds.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "min": r.get("minQuantity", 1),
                "max": r.get("maxQuantity", 1),
                "choices": choices,
            }
        )
    return {
        "code": code,
        "name": name,
        "price_cents": price,
        "rounds": out_rounds,
        "image": detail.get("image"),
    }


def save_snapshot(snapshot: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _map_parallel(items, fn, workers: int = 8, token: str | None = None, pause: float = 0.0):
    """按 SKU 并发跑 fn(code, client)，产出 (code, result, error)。

    每个线程拿自己的 client：MCP 客户端持有 session id 与自增 id，
    共用一份在并发下会互相踩。失败重试两次，仍失败就返回错误让调用方记日志。
    """
    from concurrent.futures import ThreadPoolExecutor

    if workers <= 1 or len(items) <= 1:
        client = MCDMCPClient(token)
        for code in items:
            try:
                yield code, fn(code, client), None
            except Exception as exc:  # noqa: BLE001
                yield code, None, str(exc)
            if pause:
                time.sleep(pause)
        return

    local = threading.local()

    def client() -> MCDMCPClient:
        cl = getattr(local, "cl", None)
        if cl is None:
            cl = MCDMCPClient(token)
            local.cl = cl
        return cl

    def run(code):
        last = ""
        for attempt in range(3):
            try:
                return code, fn(code, client()), None
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                if "429" not in last and attempt == 0:
                    break  # 非限流错误没必要重试
                time.sleep(0.8 * (attempt + 1))
        return code, None, last

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for code, result, err in pool.map(run, items):
            yield code, result, err
