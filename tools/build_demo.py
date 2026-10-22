"""把门店快照编译成可离线打开的审计页面。

为什么要有这一步：
评审或路人打开一个 Python 项目，第一件事不是装依赖跑 CLI，
而是想知道「这玩意儿到底说了什么」。所以把审计结果编译成一个
零依赖、双击就能开的单文件 HTML，数据全部内嵌（避免 file:// 的 CORS 问题）。

用法：
    python tools/build_demo.py
    python tools/build_demo.py --data data/store_hangzhou.json --out demo/index.html
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcd_solver.audit import audit_all, summarize  # noqa: E402
from mcd_solver.catalog import load_snapshot  # noqa: E402
from mcd_solver.decompiler import load_combos  # noqa: E402


def build_payload(snapshot: dict) -> dict:
    combos, _ = load_combos(snapshot)
    audits = audit_all(combos, snapshot["items"])
    stats = summarize(audits)

    rows = []
    for a in audits:
        rows.append(
            {
                "name": a.combo.name,
                "combo": a.combo.price_cents,
                "single": a.single_cents,
                "diff": a.diff_cents,
                "discount": round(a.discount * 10, 1) if a.discount else None,
                "verdict": a.verdict,
                "parts": [[n, p] for n, p in a.parts],
                "missing": a.missing,
            }
        )
    # 差价从小到大：真折扣在前，反向溢价在后，不可比的垫底
    rows.sort(key=lambda r: (r["diff"] if r["diff"] is not None else 10**9))

    meta = snapshot.get("meta", {})
    return {
        "meta": {
            "store": meta.get("store", ""),
            "store_code": meta.get("store_code", ""),
            "city": meta.get("city", ""),
            "fetched_at": meta.get("fetched_at", ""),
            "source": meta.get("source", ""),
        },
        "stats": {
            "total": stats["total"],
            "comparable": stats["comparable"],
            "zero": stats["zero"],
            "real": stats["real"],
            "reverse": stats["reverse"],
            "unknown": stats["unknown"],
            "zero_ratio": round(stats["zero_ratio"] * 100),
            "savings": stats["total_savings_cents"],
        },
        "rows": rows,
        "items_count": len(snapshot.get("items", [])),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="编译离线审计页面")
    parser.add_argument("--data", default=str(ROOT / "data" / "store_hangzhou.json"))
    parser.add_argument("--out", default=str(ROOT / "demo" / "index.html"))
    args = parser.parse_args(argv)

    snapshot = load_snapshot(args.data)
    payload = build_payload(snapshot)

    template = (ROOT / "tools" / "demo_template.html").read_text(encoding="utf-8")
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = template.replace("__AUDIT_DATA__", blob)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    s = payload["stats"]
    print(f"已生成：{out}")
    print(
        f"  门店 {payload['meta']['store']}｜套餐 {s['total']} 个｜可比 {s['comparable']} 个"
        f"｜零折扣 {s['zero']} 个（{s['zero_ratio']}%）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
