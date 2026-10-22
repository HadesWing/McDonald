"""本地实时服务：把静态审计页 + 一个现场抓取接口一起跑起来。

为什么需要它：
线上托管的是构建期生成的静态快照（打开快、评审零配置），但价格是会变的。
想要「现在这一刻这家店卖多少钱」，必须有人真的去调 MCP——浏览器不能直连
（MCP 端点不返回 CORS 头），也不该把 token 放进网页。所以做成本地一条命令：

    python mcd.py serve
    → http://127.0.0.1:8765

页面打开时探一下 /api/health：探到了就显示「实时抓取」按钮，探不到就退回静态快照，
两种情况用的是同一份 docs/index.html。
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .compare import store_payload
from .fetch import fetch_snapshot
from .geo import guess_city, search_stores
from .mcp_client import load_local_secrets
from .pricing import compare_combo

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
DISTRICTS = ROOT / "data" / "districts.json"

_CACHE: dict[tuple[str, str, str], tuple[float, dict]] = {}
_CACHE_TTL = 180.0
_LOCK = threading.Lock()


def districts() -> dict:
    """预置的城市关键词候选。文件不存在就返回空表，前端自动退化成纯输入框。"""
    try:
        return json.loads(DISTRICTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def short_label(store_name: str) -> str:
    """「麦当劳杭州灵隐景区餐厅」→「灵隐景区」。用于页面 tab 标签。"""
    name = store_name or "门店"
    for prefix in ("麦当劳", "McDonald's", "McDonalds"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    for city in ("杭州", "上海", "北京", "深圳", "广州"):
        if name.startswith(city):
            name = name[len(city) :]
    for suffix in ("餐厅", "门店", "店"):
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            name = name[: -len(suffix)]
            break
    return name or store_name


def live_audit(city: str, keyword: str, workers: int = 8, store_code: str | None = None) -> dict:
    key = (city, keyword, store_code or "")
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            payload = dict(hit[1])
            payload["cached"] = True
            return payload

    snapshot = fetch_snapshot(city, keyword, workers=workers, store_code=store_code)
    payload = store_payload(snapshot, short_label(snapshot["meta"].get("store", "")))

    with _LOCK:
        _CACHE[key] = (time.time(), payload)
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "ComboAssassin/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        return

    # ---------- 工具 ----------

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._json({"error": "not found", "path": str(path)}, 404)
            return
        body = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/api/health":
            has_token = bool(os.environ.get("MCD_MCP_TOKEN"))
            self._json({"ok": True, "token": has_token, "cached": len(_CACHE)})
            return

        if route == "/api/geo":
            # 按出口 IP 猜城市，用于预填搜索框；拿不到就让前端留空，不编。
            self._json({"ok": True, "location": guess_city()})
            return

        if route == "/api/districts":
            q = parse_qs(parsed.query)
            city = (q.get("city") or [""])[0]
            table = districts()
            self._json({"ok": True, "cities": [k for k in table if not k.startswith("_")], "areas": table.get(city) or []})
            return

        if route == "/api/combo":
            # 选中一个套餐，看附近几家店各卖多少钱。每家只询一次价，比抓整份菜单快两个数量级。
            q = parse_qs(parsed.query)
            code = (q.get("code") or [""])[0].strip()
            city = (q.get("city") or ["杭州"])[0]
            keyword = (q.get("keyword") or [""])[0].strip()
            codes = [c for c in (q.get("stores") or [""])[0].split(",") if c.strip()]
            if not code:
                self._json({"error": "缺少套餐编码"}, 400)
                return
            if not os.environ.get("MCD_MCP_TOKEN"):
                self._json({"error": "服务端没有配置 MCD_MCP_TOKEN"}, 503)
                return
            t0 = time.time()
            try:
                listed = search_stores(city, keyword) if keyword else []
                known = {str(s["storeCode"]): s for s in listed}
                picked = [known[c] for c in codes if c in known] or listed
                if not picked:
                    picked = [{"storeCode": c, "storeName": c} for c in codes]
                result = compare_combo(code, picked)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 500)
                return
            result["elapsed_ms"] = int((time.time() - t0) * 1000)
            self._json({"ok": True, **result})
            return

        if route == "/api/stores":
            q = parse_qs(parsed.query)
            city = (q.get("city") or [""])[0].strip()
            keyword = (q.get("keyword") or [""])[0].strip()
            be_type = int((q.get("beType") or ["1"])[0])
            if not os.environ.get("MCD_MCP_TOKEN"):
                self._json({"error": "本地没有配置 MCD_MCP_TOKEN，门店搜索不可用"}, 503)
                return
            if not city and not keyword:
                self._json({"error": "城市和关键词至少要给一个"}, 400)
                return
            try:
                stores = search_stores(city or "杭州", keyword, be_type=be_type)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 500)
                return
            self._json({"ok": True, "city": city, "keyword": keyword, "stores": stores})
            return

        if route == "/api/audit":
            q = parse_qs(parsed.query)
            city = (q.get("city") or ["杭州"])[0]
            keyword = (q.get("keyword") or ["西湖"])[0]
            store_code = (q.get("storeCode") or [None])[0]
            if not os.environ.get("MCD_MCP_TOKEN"):
                self._json({"error": "本地没有配置 MCD_MCP_TOKEN，实时抓取不可用"}, 503)
                return
            t0 = time.time()
            try:
                payload = live_audit(city, keyword, store_code=store_code)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 500)
                return
            payload["elapsed_ms"] = int((time.time() - t0) * 1000)
            payload["keyword"] = keyword
            self._json({"ok": True, "store": payload})
            return

        rel = unquote(route).lstrip("/") or "index.html"
        target = (DOCS / rel).resolve()
        try:
            target.relative_to(DOCS.resolve())
        except ValueError:
            self._json({"error": "forbidden"}, 403)
            return
        self._file(target)


def serve(host: str | None = None, port: int | None = None) -> None:
    """本地跑：默认 127.0.0.1:8765；云端跑：读 $PORT 并绑 0.0.0.0。

    两种场景共用同一个 Handler，所以线上演示看到的就是本地这版，不是另写一份。
    """
    load_local_secrets()
    env_port = os.environ.get("PORT")
    if port is None:
        port = int(env_port or 8765)
    if host is None:
        host = os.environ.get("APP_HOST") or ("0.0.0.0" if env_port else "127.0.0.1")

    httpd = ThreadingHTTPServer((host, port), Handler)
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print("套餐刺客 · 服务已启动")
    print(f"  {host}:{port}  →  http://{shown}:{port}/")
    if not os.environ.get("MCD_MCP_TOKEN"):
        print("  ⚠️  没有 MCD_MCP_TOKEN，实时抓取会失败（静态快照仍可看）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
