"""跨门店对比：同一份套餐，在不同门店卖多少钱。

为什么需要这个模块：
单店审计只能回答「这家店的套餐有没有便宜」，但麦当劳同一份套餐（同名同编码）
在不同门店的价格并不一致。把多家店的审计结果并起来看，才能回答
「我该去哪家店点」「这家店是不是整体偏贵」。

这里同时提供 web demo 需要的序列化结果（store_payload / build_cross），
CLI 的 compare 子命令和 tools/build_demo.py 共用同一份计算，避免两边算得不一样。
"""

from __future__ import annotations

from typing import Any, Sequence

from .audit import audit_all, summarize
from .catalog import parse_snapshot
from .decompiler import load_combos


def store_payload(snapshot: dict, label: str) -> dict:
    """把一家门店的快照算成可序列化的审计结果。

    兼容两种输入：磁盘快照的原始 dict，或已经 parse 过的运行期结构。
    """
    raw_items = snapshot.get("items") or []
    catalog = snapshot if raw_items and not isinstance(raw_items[0], dict) else parse_snapshot(snapshot)
    combos, _ = load_combos(catalog)
    audits = audit_all(combos, catalog["items"])
    stats = summarize(audits)

    rows: list[dict[str, Any]] = []
    for a in audits:
        rows.append(
            {
                "name": a.combo.name,
                "code": a.combo.code,
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
        "label": label,
        "meta": {
            "store": meta.get("store", ""),
            "store_code": meta.get("store_code", ""),
            "city": meta.get("city", ""),
            "address": meta.get("address", ""),
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


def _part_names(row: dict) -> tuple[str, ...]:
    return tuple(str(p[0]) for p in row.get("parts") or [])


def build_cross(stores: Sequence[dict]) -> dict:
    """同名套餐跨店比价。只在 ≥2 家门店都出现的套餐参与。"""
    if len(stores) < 2:
        return {"rows": [], "stats": {"shared": 0, "diff": 0, "max_diff": 0, "stores": len(stores)}}

    index: dict[str, dict[str, dict]] = {}
    for s in stores:
        for r in s["rows"]:
            index.setdefault(r["name"], {})[s["label"]] = r

    rows: list[dict[str, Any]] = []
    for name, by_store in index.items():
        if len(by_store) < 2:
            continue
        prices = {lab: r["combo"] for lab, r in by_store.items()}
        lo_lab = min(prices, key=lambda k: prices[k])
        hi_lab = max(prices, key=lambda k: prices[k])
        parts = {_part_names(r) for r in by_store.values()}
        rows.append(
            {
                "name": name,
                "prices": prices,
                "diff": prices[hi_lab] - prices[lo_lab],
                "cheap": lo_lab,
                "dear": hi_lab,
                "cheap_price": prices[lo_lab],
                "dear_price": prices[hi_lab],
                "same_parts": len(parts) == 1,
                # 同店名套餐的「套餐 vs 单点」折扣率，按店列出，方便看哪家真的在打折
                "rates": {
                    lab: (round(-r["diff"] / r["single"] * 100, 1) if r["diff"] is not None and r["single"] else None)
                    for lab, r in by_store.items()
                },
            }
        )

    rows.sort(key=lambda r: -r["diff"])
    differing = [r for r in rows if r["diff"] > 0]
    max_diff = differing[0]["diff"] if differing else 0
    return {
        "rows": rows,
        "stats": {
            "stores": len(stores),
            "shared": len(rows),
            "diff": len(differing),
            "same_price": len(rows) - len(differing),
            "max_diff": max_diff,
            "composition_diff": sum(1 for r in rows if not r["same_parts"]),
        },
    }


def render_cross(cross: dict, labels: Sequence[str]) -> str:
    """CLI 用的跨店比价表。"""
    rows = cross["rows"]
    if not rows:
        return "没有可跨店对比的套餐（需要至少两家门店，且存在同名套餐）。"

    head = f"{'套餐':<26}" + "".join(f"{lab:>10}" for lab in labels) + f"{'最大差':>10}"
    lines = [head, "-" * len(head)]
    for r in rows[:30]:
        cells = "".join(
            (f"{r['prices'][lab] / 100:>10.2f}" if lab in r["prices"] else f"{'—':>10}") for lab in labels
        )
        lines.append(f"{r['name'][:24]:<26}{cells}{r['diff'] / 100:>10.2f}")
    if len(rows) > 30:
        lines.append(f"… 另有 {len(rows) - 30} 个同名套餐，见完整数据")
    s = cross["stats"]
    lines.append("")
    lines.append(
        f"共 {s['shared']} 个同名套餐在 ≥2 家门店出现，其中 {s['diff']} 个价格不同"
        f"（{s['same_price']} 个一致），最大价差 {s['max_diff'] / 100:.2f} 元"
    )
    if s["composition_diff"]:
        lines.append(f"另有 {s['composition_diff']} 个同名套餐的默认配置在不同门店不一致")
    return "\n".join(lines)
