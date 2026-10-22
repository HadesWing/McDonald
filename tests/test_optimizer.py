"""求解器单元测试：run `python -m pytest tests -q` 或 `python tests/test_optimizer.py`。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcd_solver.catalog import load_snapshot, normalize_category
from mcd_solver.models import Coupon, DeliveryRule, MenuItem
from mcd_solver.optimizer import ComboSolver, min_reachable_at_least

DATA = Path(__file__).resolve().parents[1] / "data" / "sample_menu.json"


def _snap():
    return load_snapshot(DATA)


def test_bitset_min_reachable():
    prices = [800, 800, 900]
    assert min_reachable_at_least(prices, 0) == 0
    assert min_reachable_at_least(prices, 800) == 800
    assert min_reachable_at_least(prices, 810) == 900
    assert min_reachable_at_least(prices, 1600) == 1600
    assert min_reachable_at_least(prices, 1700) == 1700  # 800 + 900
    assert min_reachable_at_least(prices, 2400) == 2500  # 800+800+900
    assert min_reachable_at_least(prices, 2501) is None


def test_bitset_respects_single_use():
    # 只有一件 400，不能凑出 800
    assert min_reachable_at_least([400], 800) is None


def test_category_normalize():
    assert normalize_category("汉堡") == "staple"
    assert normalize_category("饮料") == "drink"
    assert normalize_category("甜品") == "dessert"
    assert normalize_category("谷物") == "other"


def test_giant_burger_gap_and_best_plan():
    s = _snap()
    bigmac = s["by_name"]["巨无霸"]
    solver = ComboSolver([(bigmac, 1)], s["items"], s["coupons"], s["delivery"])
    r = solver.solve()

    assert r["baseline"].subtotal_cents == 2450
    assert r["gap_to_free_delivery"] == 550
    # 基线：2450 + 9 元配送费
    assert r["baseline"].pay_cents == 3350

    best = r["best"]
    # 加一瓶 8 元小可乐 -> 小计 32.5，触发「满 30 减 5」-> 实付 27.5
    assert best.subtotal_cents == 3250
    assert best.discount_cents == 500
    assert best.delivery_cents == 0
    assert best.pay_cents == 2750
    assert best.pay_cents < r["baseline"].pay_cents  # 加东西反而更便宜


def test_no_add_when_fee_is_cheap():
    """配送费只有 2 元、且最便宜的加购品要 4 元时，结论应该是「不用凑」。"""
    s = _snap()
    items = s["items"]
    cart = [(it, 1) for it in items if it.name == "大薯条"]  # 14.5
    cheap_items = [it for it in items if it.price_cents >= 400]
    solver = ComboSolver(
        cart,
        cheap_items,
        coupons=[],
        delivery=DeliveryRule(fee_cents=200, free_threshold_cents=2000),
    )
    r = solver.solve()
    assert r["best"] is r["baseline"]
    assert r["best"].pay_cents == 1650


def test_allow_categories_filter():
    s = _snap()
    bigmac = s["by_name"]["巨无霸"]
    solver = ComboSolver(
        [(bigmac, 1)],
        s["items"],
        s["coupons"],
        s["delivery"],
        allow_categories=["drink"],
    )
    assert all(it.category == "drink" for it in solver.pool)
    r = solver.solve()
    assert r["best"].pay_cents == 2750  # 小可乐仍是最优


def test_exclude_filter():
    s = _snap()
    bigmac = s["by_name"]["巨无霸"]
    cola = s["by_name"]["可口可乐 小"]
    solver = ComboSolver(
        [(bigmac, 1)], s["items"], s["coupons"], s["delivery"], exclude_codes=[cola.code]
    )
    assert all(it.code != cola.code for it in solver.pool)
    r = solver.solve()
    # 去掉 8 元可乐后，最优退化为 8 元苹果派（同价）
    assert r["best"].subtotal_cents == 3250


def test_kcal_cap():
    s = _snap()
    bigmac = s["by_name"]["巨无霸"]
    solver = ComboSolver(
        [(bigmac, 1)], s["items"], s["coupons"], s["delivery"], kcal_cap=100
    )
    assert all((it.kcal or 0) <= 100 for it in solver.pool)
    r = solver.solve()
    assert r["best"].kcal <= 550 + 100


def test_empty_cart():
    s = _snap()
    solver = ComboSolver([], s["items"], s["coupons"], s["delivery"])
    r = solver.solve()
    assert r["best"].pay_cents >= 0


def test_quantity_in_cart():
    s = _snap()
    bigmac = s["by_name"]["巨无霸"]
    solver = ComboSolver([(bigmac, 2)], s["items"], s["coupons"], s["delivery"])
    r = solver.solve()
    assert r["baseline"].subtotal_cents == 4900
    assert r["best"].delivery_cents == 0


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
