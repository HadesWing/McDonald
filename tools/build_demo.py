"""把多家门店快照编译成可离线打开、也可连本地服务实时刷新的审计页面。

为什么要有这一步：
评审或路人打开一个 Python 项目，第一件事不是装依赖跑 CLI，
而是想知道「这玩意儿到底说了什么」。所以把审计结果编译成一个
零依赖、双击就能开的单文件 HTML，数据全部内嵌（避免 file:// 的 CORS 问题）。

用法：
    python tools/build_demo.py                       # 自动吃 data/store_*.json
    python tools/build_demo.py --store data/store_hangzhou.json:灵隐景区店
    python tools/build_demo.py --out docs/index.html
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcd_solver.catalog import load_snapshot  # noqa: E402
from mcd_solver.compare import build_cross, store_payload  # noqa: E402

# 门店标签：文件名 -> 人话。没有命中的用门店名兜底。
LABELS = {
    "store_hangzhou": "灵隐景区店",
    "store_wulin": "国大广场店",
    "store_binjiang": "滨江店",
}


def _default_stores() -> list[tuple[Path, str]]:
    # 展示顺序：景区店放第一个（故事从它开始），其余按商圈排
    order = ["store_hangzhou", "store_wulin", "store_binjiang"]
    paths = [p for p in sorted((ROOT / "data").glob("store_*.json")) if not p.name.startswith("_")]
    paths.sort(key=lambda p: (order.index(p.stem) if p.stem in order else len(order), p.stem))
    return [(p, LABELS.get(p.stem, p.stem)) for p in paths]


def build_payload(stores: list[tuple[Path, str]]) -> dict:
    payloads = []
    for path, label in stores:
        payloads.append(store_payload(load_snapshot(path), label))
    cross = build_cross(payloads)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "stores": payloads,
        "cross": cross,
        "mode": "static",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="编译离线审计页面")
    parser.add_argument(
        "--store",
        action="append",
        metavar="PATH[:LABEL]",
        help="门店快照路径，可重复；不给则自动吃 data/store_*.json",
    )
    parser.add_argument("--out", default=str(ROOT / "docs" / "index.html"))
    args = parser.parse_args(argv)

    if args.store:
        stores = []
        for spec in args.store:
            raw, _, label = spec.partition(":")
            p = Path(raw)
            stores.append((p, label or LABELS.get(p.stem, p.stem)))
    else:
        stores = _default_stores()

    if not stores:
        print("没有找到门店快照，先跑 python mcd.py fetch --city 杭州 --keyword 西湖")
        return 1

    payload = build_payload(stores)
    template = (ROOT / "tools" / "demo_template.html").read_text(encoding="utf-8")
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = template.replace("__AUDIT_DATA__", blob)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    c = payload["cross"]["stats"]
    print(f"已生成：{out}")
    print(f"  门店 {len(payload['stores'])} 家｜同名套餐 {c['shared']} 个｜价格不同 {c['diff']} 个")
    print(f"  最大价差 {c['max_diff'] / 100:.2f} 元")
    for s in payload["stores"]:
        st = s["stats"]
        print(
            f"  - {s['label']}：套餐 {st['total']}｜可比 {st['comparable']}"
            f"｜零折扣 {st['zero']}（{st['zero_ratio']}%）"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
