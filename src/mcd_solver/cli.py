"""命令行入口：mcd-solver

零第三方依赖，可直接 `python mcd.py solve --cart 巨无霸` 运行。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .catalog import load_snapshot
from .geo import CST
from .models import MenuItem, yuan
from .optimizer import ComboSolver, explain

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data" / "sample_menu.json"
DEFAULT_STORE = Path(__file__).resolve().parents[2] / "data" / "store_hangzhou.json"


def _resolve_cart(snapshot: dict, names: list[str]) -> list[tuple[MenuItem, int]]:
    """把用户输入的名字/编码解析成购物车条目，支持 `名称x2` 写法。"""
    cart: list[tuple[MenuItem, int]] = []
    for raw in names:
        qty = 1
        token = raw.strip()
        if "x" in token.lower():
            head, _, tail = token.lower().rpartition("x")
            if tail.isdigit():
                token, qty = token[: len(head)], int(tail)
        item = snapshot["by_name"].get(token) or snapshot["by_code"].get(token)
        if item is None:
            # 退回模糊匹配（包含即命中，取最短的那个名字）
            hits = [it for name, it in snapshot["by_name"].items() if token in name]
            if not hits:
                raise SystemExit(f"找不到餐品：{raw}")
            item = min(hits, key=lambda it: len(it.name))
        cart.append((item, qty))
    return cart


def cmd_solve(args: argparse.Namespace) -> int:
    snapshot = load_snapshot(args.data)
    cart = _resolve_cart(snapshot, args.cart)

    solver = ComboSolver(
        cart=cart,
        candidates=snapshot["items"],
        coupons=[] if args.no_coupon else snapshot["coupons"],
        delivery=snapshot["delivery"],
        max_added=args.max_added,
        kcal_cap=args.kcal_cap,
        allow_categories=[c.strip() for c in args.allow.split(",")] if args.allow else None,
        exclude_codes=args.exclude.split(",") if args.exclude else (),
    )
    result = solver.solve()

    meta = snapshot["meta"]
    print(f"门店：{meta.get('store', '未知')}｜数据源：{meta.get('source', '未知')}")
    print("=" * 56)
    print(explain(result))
    print("=" * 56)
    if args.verbose:
        print("\n全部候选方案：")
        for p in result["all"]:
            print(
                f"  {yuan(p.pay_cents):>6} 元｜{p.kcal:>5} kcal｜小计 {yuan(p.subtotal_cents):>6}"
                f"｜券 {p.coupon.name if p.coupon else '无'}｜{p.summary()}"
            )
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    """列出麦当劳 MCP 提供的全部工具及其入参，便于对接与排查。"""
    from .mcp_client import MCDMCPClient

    client = MCDMCPClient(args.token, endpoint=args.endpoint)
    tools = client.list_tools()
    print(f"共 {len(tools)} 个工具：")
    for t in tools:
        schema = t.get("inputSchema") or {}
        props = sorted((schema.get("properties") or {}).keys())
        required = set(schema.get("required") or [])
        names = [f"{p}*" if p in required else p for p in props]
        print(f"  · {t['name']:<26} {t.get('description', '')[:48]}")
        if names:
            print(f"      params: {', '.join(names)}")
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    """直接调用单个工具，原样打印返回，用于取数与调试。"""
    import json

    from .mcp_client import MCDMCPClient

    client = MCDMCPClient(args.token, endpoint=args.endpoint)
    payload = json.loads(args.args) if args.args else {}
    result = client.call_tool(args.tool, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2)[: args.limit])
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """审计门店全部套餐：套餐价 vs 单品原价合计。"""
    from .audit import audit_all, explain_audit, render_table, summarize
    from .decompiler import load_combos

    snapshot = load_snapshot(args.data)
    combos, _ = load_combos(snapshot)
    audits = audit_all(combos, snapshot["items"])

    meta = snapshot["meta"]
    print("=" * 68)
    print(explain_audit(audits, meta.get("store", "")))
    print("=" * 68)
    print(render_table(audits, only_comparable=not args.all))
    print("=" * 68)
    print(f"单价来源：{meta.get('source', 'unknown')}｜抓取时间：{meta.get('fetched_at', '')}")

    if args.verify:
        _verify_online(snapshot, audits, args.verify)
    return 0


def _verify_online(snapshot: dict, audits: list, limit: int) -> None:
    """拿官方 calculate-price 现场复核前 N 个结论，证明不是本地估算。"""
    from .mcp_client import MCDMCPClient
    from .models import yuan

    meta = snapshot["meta"]
    scope = {
        "storeCode": meta["store_code"],
        "orderType": meta.get("order_type", 1),
        "beType": meta.get("be_type", 1),
    }
    client = MCDMCPClient()

    def price(items: list[dict]) -> int | None:
        data = client.call_tool("calculate-price", {**scope, "items": items}).get("data") or {}
        return data.get("price")

    rows = [a for a in audits if a.comparable]
    rows.sort(key=lambda a: a.diff_cents or 0)
    sample = rows[:limit]
    print(f"\n官方复核（取差价最小的 {len(sample)} 个，重新向 calculate-price 询价）：")
    ok = 0
    checked = 0
    for a in sample:
        codes = []
        for name, _ in a.parts:
            hit = next((it for it in snapshot["items"] if it.name == name), None)
            if hit:
                codes.append(hit.code)
        if len(codes) != len(a.parts):
            continue
        single = price([{"productCode": c, "quantity": 1} for c in codes])
        combo_now = price([{"productCode": a.combo.code, "quantity": 1}])
        match = single == a.single_cents and combo_now == a.combo.price_cents
        checked += 1
        ok += 1 if match else 0
        print(
            f"  {'✓' if match else '✗'} {a.combo.name}｜官方套餐 {yuan(combo_now or 0)}"
            f" / 官方单点 {yuan(single or 0)}｜本地 {yuan(a.combo.price_cents)} / {yuan(a.single_cents or 0)}"
        )
    print(f"  一致性：{ok}/{checked}")


def cmd_slot(args: argparse.Namespace) -> int:
    """把套餐拆成「主食 + 配菜位」，看配菜到底被定了多少钱。"""
    from .audit import audit_all, explain_slots, render_slots, slot_pricing
    from .decompiler import load_combos

    snapshot = load_snapshot(args.data)
    combos, _ = load_combos(snapshot)
    stats = slot_pricing(audit_all(combos, snapshot["items"]))

    meta = snapshot["meta"]
    print("=" * 76)
    print(f"门店：{meta.get('store', '')}")
    print(explain_slots(stats))
    print("=" * 76)
    print(render_slots(stats))
    print("=" * 76)
    print("配菜位 = 套餐价 − 主食单点价；配菜单点 = 配菜各单品在门店单点合计。")
    return 0


def cmd_locate(args: argparse.Namespace) -> int:
    """到店锁店：列出候选门店，并告诉你现在开着没有。"""
    from .geo import guess_city, search_stores

    city = args.city
    if not city:
        loc = guess_city()
        if loc:
            city = loc["city"]
            print(f"按出口 IP 猜的城市：{city}（{loc.get('region') or ''}）——不准就直接用 --city 指定")
        else:
            print("拿不到城市，请用 --city 指定，例如 --city 杭州")
            return 2

    stores = search_stores(city, args.keyword, be_type=args.be_type, token=args.token)
    if not stores:
        print(f"没搜到门店：{city} {args.keyword}")
        return 1

    now = datetime.now(CST)
    print(f"\n{now:%H:%M} · {city}「{args.keyword}」附近 {len(stores)} 家\n")
    for i, s in enumerate(stores):
        if s["open_now"] is None:
            mark, state = "·", "营业时间未知"
        elif s["open_now"]:
            left = int(s["minutes_left"] or 0)
            h, m = divmod(left, 60)
            state = f"营业中，还有 {h} 小时 {m} 分关门" if h else f"营业中，还有 {m} 分钟关门"
            mark = "!" if s["closing_soon"] else "●"
        else:
            mark, state = "○", "已打烊"
        extra = " · 可预约" if s["reservation"] else ""
        print(f"  {mark} [{i}] {s['storeName']}")
        print(f"        {s['businessStartTime']}–{s['businessEndTime']}  {state}{extra}")
        print(f"        {s['address']}  (编码 {s['storeCode']})")

    if any(s["closing_soon"] for s in stores):
        print("\n  ! 有门店 45 分钟内打烊，别走错门。")
    print("\n  distance 字段没打印：MCP 返回的是「到你输入的关键词那个地点」的距离，不是到你。")
    print(f"  选好之后跑：python mcd.py fetch --city {city} --keyword {args.keyword} --store-code <编码>")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """从麦当劳 MCP 拉取真实门店快照。"""
    from .fetch import fetch_snapshot, save_snapshot

    snapshot = fetch_snapshot(
        city=args.city,
        keyword=args.keyword,
        store_index=args.index,
        order_type=args.order_type,
        be_type=args.be_type,
        token=args.token,
        store_code=getattr(args, "store_code", None),
    )
    path = save_snapshot(snapshot, args.out)
    print(f"已保存：{path}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """跨门店比价：同一份套餐，各家卖多少钱。"""
    from .compare import build_cross, render_cross, store_payload

    stores = []
    for spec in args.data:
        raw, _, label = spec.partition(":")
        p = Path(raw)
        label = label or p.stem.replace("store_", "")
        stores.append(store_payload(load_snapshot(p), label))

    if len(stores) < 2:
        print("至少给两家门店：--data a.json --data b.json")
        return 1

    print("=" * 78)
    for s in stores:
        st = s["stats"]
        print(
            f"{s['label']:<10} {s['meta'].get('store', '')}"
            f"｜套餐 {st['total']}｜可比 {st['comparable']}｜零折扣 {st['zero']}（{st['zero_ratio']}%）"
        )
    print("=" * 78)
    print(render_cross(build_cross(stores), [s["label"] for s in stores]))
    print("=" * 78)
    print("口径：只比同名套餐；两边都必须是官方 calculate-price 报价。")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """起一个本地服务：静态页 + 现场抓取接口。"""
    from .serve import serve

    serve(host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcd-solver",
        description="套餐刺客：把套餐拆开算一遍，看它到底有没有便宜。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("solve", help="求解最优凑单方案")
    p.add_argument("--cart", nargs="+", required=True, help="购物车已有餐品，支持 `名称x份数`")
    p.add_argument("--data", default=str(DEFAULT_DATA), help="菜单快照 JSON 路径")
    p.add_argument("--max-added", type=int, default=5, help="最多加购件数（默认 5）")
    p.add_argument("--kcal-cap", type=int, default=None, help="单件加购餐品的热量上限")
    p.add_argument("--allow", default=None, help="只允许加这些大类，如 `drink,side`")
    p.add_argument("--exclude", default=None, help="排除餐品编码，逗号分隔")
    p.add_argument("--no-coupon", action="store_true", help="不考虑优惠券")
    p.add_argument("-v", "--verbose", action="store_true", help="输出全部候选方案")
    p.set_defaults(func=cmd_solve)

    a = sub.add_parser("audit", help="审计门店全部套餐：套餐价 vs 单品原价合计")
    a.add_argument("--data", default=str(DEFAULT_STORE), help="门店快照 JSON 路径")
    a.add_argument("--all", action="store_true", help="连不可比的套餐一起列出")
    a.add_argument("--verify", type=int, default=0, metavar="N", help="用官方接口现场复核前 N 个结论")
    a.set_defaults(func=cmd_audit)

    s = sub.add_parser("slot", help="拆解配菜位定价：套餐价 − 主食单价 = 配菜花了多少钱")
    s.add_argument("--data", default=str(DEFAULT_STORE), help="门店快照 JSON 路径")
    s.set_defaults(func=cmd_slot)

    lc = sub.add_parser("locate", help="到店锁店：列候选门店，并告知现在开着没有")
    lc.add_argument("--city", default=None, help="城市名；不填则按出口 IP 猜")
    lc.add_argument("--keyword", default="", help="商圈 / 路名 / 门店名关键词")
    lc.add_argument("--be-type", type=int, default=1, choices=[1, 5], help="1-到店自取（默认） 5-得来速")
    lc.add_argument("--token", default=None)
    lc.set_defaults(func=cmd_locate)

    f = sub.add_parser("fetch", help="从麦当劳 MCP 拉取真实门店快照")
    f.add_argument("--city", required=True)
    f.add_argument("--keyword", required=True)
    f.add_argument("--index", type=int, default=0, help="同名门店取第几个")
    f.add_argument("--store-code", default=None, help="门店编码，指定后精确命中（到店场景用 locate 拿）")
    f.add_argument("--order-type", type=int, default=1, choices=[1, 2])
    f.add_argument("--be-type", type=int, default=1, choices=[1, 2, 5, 6])
    f.add_argument("--out", default=str(DEFAULT_STORE))
    f.add_argument("--token", default=None)
    f.set_defaults(func=cmd_fetch)

    t = sub.add_parser("tools", help="列出麦当劳 MCP 的全部工具")
    t.add_argument("--token", default=None, help="MCP Token，缺省读环境变量 MCD_MCP_TOKEN")
    t.add_argument("--endpoint", default="https://mcp.mcd.cn")
    t.set_defaults(func=cmd_tools)

    c = sub.add_parser("call", help="直接调用某个 MCP 工具，原样输出结果")
    c.add_argument("tool", help="工具名，如 query-meals")
    c.add_argument("--args", default=None, help="JSON 字符串参数")
    c.add_argument("--token", default=None)
    c.add_argument("--endpoint", default="https://mcp.mcd.cn")
    c.add_argument("--limit", type=int, default=4000, help="输出截断长度")
    c.set_defaults(func=cmd_call)

    cp = sub.add_parser("compare", help="跨门店比价：同一份套餐各家卖多少钱")
    cp.add_argument(
        "--data",
        action="append",
        required=True,
        metavar="PATH[:LABEL]",
        help="门店快照，可重复，至少两个；支持 path:标签",
    )
    cp.set_defaults(func=cmd_compare)

    sv = sub.add_parser("serve", help="起本地实时服务：静态页 + 现场抓取接口")
    sv.add_argument("--host", default=None, help="默认 127.0.0.1；设了 $PORT 则绑 0.0.0.0")
    sv.add_argument("--port", type=int, default=None, help="默认 8765，或读环境变量 PORT")
    sv.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
