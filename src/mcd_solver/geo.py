"""到店定位：把「我现在在哪家店」这件事做到能用的程度。

先说清楚 MCP 的能力边界（都是实测结论，不是猜的）：

- `query-nearby-stores` 的入参只有 `beType / searchType / city / keyword`，
  **没有经纬度**。只传 searchType 会直接报 `600058 城市名或者关键词不能为空`。
- 返回体里的 `longitude` / `latitude` 恒为 `0.0`，门店坐标根本不下发。
- 返回体里的 `distance` 不是到你的距离，而是**到关键词解析出的那个地点**的距离：
  同一家灵隐店，搜「灵隐」是 431 米，搜「西湖」就变成 2545 米。

所以「自动算出最近的门店」在 MCP 层面做不到，硬做就是编数据。这里只做三件真事：

1. `guess_city()`：用 IP 兜底拿到城市名，省掉一次手输（城市级精度，不是门店级）。
2. `search_stores()`：按关键词列出候选门店，并补上**到店场景真正重要的信息**——
   现在是否营业、几点关门、能不能预约。20:30 走到店门口，你需要的不是「最近 800 米」，
   而是「这家已经关门了」。
3. `haversine()` / `match_known_place()`：给「到店记忆」用。浏览器拿到 GPS 坐标后，
   和上次在这家店点单时记下的坐标比对，落在半径内就自动认店——常去的店从此不用再搜。
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .mcp_client import MCDMCPClient

CST = timezone(timedelta(hours=8))
_CITY_CACHE: dict[str, tuple[float, dict]] = {}
_CITY_TTL = 1800.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """两点间球面距离，返回米。"""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(a)))


def guess_city(timeout: float = 6.0) -> dict | None:
    """按出口 IP 猜城市。只到城市级，够用来预填搜索框。

    刻意不引入地图服务：高德/腾讯的逆地理要 key，OSM Nominatim 在境内实测连不通
    （Tunnel 502）。IP 定位免费、无 key、1 秒内返回，是这个场景下性价比最高的。
    """
    hit = _CITY_CACHE.get("ip")
    if hit and time.time() - hit[0] < _CITY_TTL:
        return hit[1]

    url = "http://ip-api.com/json/?lang=zh-CN&fields=status,city,regionName,country,lat,lon"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "combo-assassin/1.0"})
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8")
        d = json.loads(raw)
        if d.get("status") != "success" or not d.get("city"):
            return None
        out = {
            "city": d["city"],
            "region": d.get("regionName"),
            "country": d.get("country"),
            "lat": d.get("lat"),
            "lon": d.get("lon"),
            "source": "ip",
        }
        _CITY_CACHE["ip"] = (time.time(), out)
        return out
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _hm(text: str | None) -> int | None:
    """「07:00」→ 7*60。解析不了就返回 None，不要瞎猜。"""
    if not text or ":" not in text:
        return None
    try:
        h, m = text.split(":", 1)
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def store_status(store: dict, now: datetime | None = None) -> dict:
    """算出「现在开着吗 / 还有多久关门」。

    只做纯时间判断，不改 MCP 的 businessStatus —— 官方字段和本地时间冲突时，
    两个都返回，让调用方自己决定信谁（跨零点营业的门店尤其容易打架）。
    """
    now = now or datetime.now(CST)
    mins = now.hour * 60 + now.minute
    start = _hm(store.get("businessStartTime"))
    end = _hm(store.get("businessEndTime"))
    open_now: bool | None = None
    minutes_left: int | None = None

    if start is not None and end is not None:
        if end > start:
            open_now = start <= mins < end
            minutes_left = end - mins if open_now else None
        else:  # 跨零点，例如 10:00–02:00
            open_now = mins >= start or mins < end
            minutes_left = (end - mins) % 1440 if open_now else None

    return {
        "open_now": open_now,
        "minutes_left": minutes_left,
        "closing_soon": bool(open_now and minutes_left is not None and minutes_left <= 45),
        "official_open": store.get("businessStatus"),
    }


def search_stores(
    city: str,
    keyword: str,
    be_type: int = 1,
    token: str | None = None,
    client: MCDMCPClient | None = None,
    limit: int = 8,
) -> list[dict]:
    """按关键词搜门店，返回带营业状态的精简列表。

    `distance` 原样带出但标注含义——它是「到关键词地点的距离」，不是到用户的距离。
    """
    c = client or MCDMCPClient(token)
    resp = c.call_tool(
        "query-nearby-stores",
        {"beType": be_type, "searchType": 2, "city": city, "keyword": keyword},
    )
    if not resp.get("success"):
        raise RuntimeError(resp.get("message") or "门店搜索失败")

    now = datetime.now(CST)
    out: list[dict] = []
    for s in (resp.get("data") or [])[:limit]:
        st = store_status(s, now)
        out.append(
            {
                "storeCode": str(s.get("storeCode")),
                "storeName": s.get("storeName"),
                "address": s.get("address"),
                "distance": s.get("distance"),
                "distance_note": "到关键词地点的距离，不是到你当前位置",
                "businessStartTime": s.get("businessStartTime"),
                "businessEndTime": s.get("businessEndTime"),
                "reservation": bool(s.get("reservation")),
                **st,
            }
        )
    return out


def match_known_place(places: list[dict], lat: float, lon: float, radius_m: int = 300) -> dict | None:
    """在「到店记忆」里找当前坐标落在哪家店的半径内。

    places 形如 [{"storeCode": "...", "label": "...", "lat": 30.2, "lon": 120.1}]，
    由前端 localStorage 维护：第一次在某家店定位并选店时，把当时的坐标记下来。
    常去的店从此打开就自动认出来——这才是到店场景真正需要的「定位」。
    """
    best: tuple[int, dict] | None = None
    for p in places or []:
        try:
            d = haversine(lat, lon, float(p["lat"]), float(p["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if d <= radius_m and (best is None or d < best[0]):
            best = (d, p)
    if not best:
        return None
    return {**best[1], "distance_m": best[0]}
