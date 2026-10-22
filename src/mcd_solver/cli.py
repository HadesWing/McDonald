"""命令行入口：mcd-solver

零第三方依赖，可直接 `python mcd.py solve --cart 巨无霸` 运行。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .catalog import load_snapshot
from .models import MenuItem, yuan
from .optimizer import ComboSolver, explain

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data" / "sample_menu.json"


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcd-solver",
        description="麦麦凑单最优解：加最少冤枉钱，精确跨过免配送费 / 券门槛。",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
