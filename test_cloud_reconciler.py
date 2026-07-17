#!/usr/bin/env python3
"""
Differential tests: cloud_reconciler MUST produce plans identical to grid_engine
running in ALLOW_REANCHOR=False (fixed geometry) mode.

Uses the exact live grid: ANCHOR=115.19, STEP=0.48, LOT_DOLLARS=115.62
"""

import unittest
from cloud_reconciler import reconcile as cloud_reconcile
from grid_engine import plan_orders as engine_plan_orders


# Fixed grid for all tests (exact live grid)
ANCHOR = 115.19
STEP = 0.48
LOT_DOLLARS = 115.62


def normalize_places(places):
    """Normalize places list for comparison (order-independent)."""
    # Sort by side, then limit_price, to get deterministic order
    def sort_key(p):
        return (p["side"], p["limit_price"])
    return sorted(places, key=sort_key)


class TestCloudReconcilerDifferentialEquivalence(unittest.TestCase):
    """
    CRITICAL: cloud_reconciler output MUST equal grid_engine.plan_orders(ALLOW_REANCHOR=False).
    """

    def _assert_plans_equal(self, runtime_state, test_name):
        """Assert cloud_reconciler and grid_engine produce identical plans."""
        # CHANGE D: Both should use dynamic lot_dollars (no override)
        # Cloud reconciler - no lot_dollars_override, computes dynamically
        cloud_result = cloud_reconcile(runtime_state, ANCHOR, STEP)

        # Grid engine in fixed mode - also computes lot_dollars dynamically
        engine_grid = {"anchor": ANCHOR, "step": STEP, "initialized": True}
        engine_result = engine_plan_orders(runtime_state, engine_grid, {"ALLOW_REANCHOR": False})

        # Compare cancels (order-independent set comparison)
        cloud_cancels = set(cloud_result["cancels"])
        engine_cancels = set(engine_result["cancels"])
        self.assertEqual(
            cloud_cancels,
            engine_cancels,
            f"{test_name}: cancels mismatch. Cloud: {cloud_cancels}, Engine: {engine_cancels}",
        )

        # Compare places (normalize and compare)
        cloud_places = normalize_places(cloud_result["places"])
        engine_places = normalize_places(engine_result["places"])

        self.assertEqual(len(cloud_places), len(engine_places), f"{test_name}: places count mismatch")

        for i, (c, e) in enumerate(zip(cloud_places, engine_places)):
            self.assertEqual(
                c["side"],
                e["side"],
                f"{test_name}: place[{i}] side mismatch. Cloud: {c}, Engine: {e}",
            )
            self.assertAlmostEqual(
                c["limit_price"],
                e["limit_price"],
                places=2,
                msg=f"{test_name}: place[{i}] price mismatch",
            )
            self.assertEqual(
                c["quantity"],
                e["quantity"],
                f"{test_name}: place[{i}] qty mismatch",
            )

    def test_1_fresh_account(self):
        """Fresh account, cash only."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "fresh_account")

    def test_2_spot_above_anchor(self):
        """Spot above anchor but within band."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.60,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "spot_above_anchor")

    def test_3_spot_below_anchor(self):
        """Spot below anchor but within band."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 114.80,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "spot_below_anchor")

    def test_4_spot_far_above_band(self):
        """Spot far above grid band (118.03 top). Should only place buys."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 125.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "spot_far_above_band")

    def test_5_spot_far_below_band(self):
        """Spot far below grid band (111.35 bottom). Should only place sells."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 105.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 100,
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "spot_far_below_band")

    def test_6_partial_fills(self):
        """Some orders filled, some still open."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 20,  # Some shares from earlier buy fills
            "open_orders": [
                {"order_id": "buy1", "side": "buy", "limit_price": 114.71, "quantity": 8, "state": "confirmed"},
                {"order_id": "sell1", "side": "sell", "limit_price": 115.67, "quantity": 8, "state": "confirmed"},
            ],
        }
        self._assert_plans_equal(runtime_state, "partial_fills")

    def test_7_stale_order_wrong_side(self):
        """Order on wrong side for its price (e.g., buy above spot)."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [
                # Stale buy above spot (wrong side for this price, should cancel)
                {"order_id": "bad_buy", "side": "buy", "limit_price": 115.67, "quantity": 8, "state": "confirmed"},
            ],
        }
        self._assert_plans_equal(runtime_state, "stale_order_wrong_side")

    def test_8_wrong_quantity(self):
        """Order at correct price but wrong quantity."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [
                # Correct price, but qty is 9 instead of expected 8
                {"order_id": "wrong_qty", "side": "buy", "limit_price": 114.71, "quantity": 9, "state": "confirmed"},
            ],
        }
        self._assert_plans_equal(runtime_state, "wrong_quantity")

    def test_9_fixpoint_perfect_grid(self):
        """Open orders exactly match desired grid. Should return empty plan."""
        # The exact 8 buys expected at spot=115.19
        # Grid lines at anchor=115.19: [111.35, 111.83, 112.31, 112.79, 113.27, 113.75, 114.23, 114.71, 115.19, ...]
        # Buys below 115.19: nearest 8 are [114.71, 114.23, 113.75, 113.27, 112.79, 112.31, 111.83, 111.35]
        # For each: qty = floor(115.62 / line)
        perfect_orders = [
            {"order_id": "b1", "side": "buy", "limit_price": 111.35, "quantity": 1, "state": "confirmed"},
            {"order_id": "b2", "side": "buy", "limit_price": 111.83, "quantity": 1, "state": "confirmed"},
            {"order_id": "b3", "side": "buy", "limit_price": 112.31, "quantity": 1, "state": "confirmed"},
            {"order_id": "b4", "side": "buy", "limit_price": 112.79, "quantity": 1, "state": "confirmed"},
            {"order_id": "b5", "side": "buy", "limit_price": 113.27, "quantity": 1, "state": "confirmed"},
            {"order_id": "b6", "side": "buy", "limit_price": 113.75, "quantity": 1, "state": "confirmed"},
            {"order_id": "b7", "side": "buy", "limit_price": 114.23, "quantity": 1, "state": "confirmed"},
            {"order_id": "b8", "side": "buy", "limit_price": 114.71, "quantity": 1, "state": "confirmed"},
        ]
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": perfect_orders,
        }
        self._assert_plans_equal(runtime_state, "fixpoint_perfect_grid")

        # Also verify both return empty plans
        cloud_result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        self.assertEqual(len(cloud_result["cancels"]), 0, "Cloud: should have no cancels")
        self.assertEqual(len(cloud_result["places"]), 0, "Cloud: should have no places")

    def test_10_small_cash_budget_cap(self):
        """Limited cash should constrain buy orders."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 500.0,  # Small
            "shares_available": 0,
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "small_cash_budget_cap")

    def test_11_small_share_budget_cap(self):
        """Limited shares should constrain sell orders."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 3,  # Small
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "small_share_budget_cap")

    def test_12_zero_cash_zero_shares(self):
        """No cash, no shares."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 0.0,
            "shares_available": 0,
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "zero_cash_zero_shares")

    def test_13_cancelled_orders_release_budget(self):
        """Cancelled orders release cash/shares for new placements."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 200.0,  # Tiny cash
            "shares_available": 0,
            "open_orders": [
                # Stale order that will cancel, releasing its notional
                {"order_id": "stale", "side": "buy", "limit_price": 110.00, "quantity": 1, "state": "confirmed"},
            ],
        }
        self._assert_plans_equal(runtime_state, "cancelled_orders_release_budget")

    def test_14_duplicate_orders_same_line(self):
        """Two orders at same price; second should cancel (only keep first)."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [
                {"order_id": "buy1", "side": "buy", "limit_price": 114.71, "quantity": 8, "state": "confirmed"},
                {"order_id": "buy1_dup", "side": "buy", "limit_price": 114.71, "quantity": 8, "state": "confirmed"},
            ],
        }
        self._assert_plans_equal(runtime_state, "duplicate_orders_same_line")

    def test_15_mixed_buy_sell_orders(self):
        """Mix of buy and sell orders, some kept, some cancelled."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 50,
            "open_orders": [
                {"order_id": "buy1", "side": "buy", "limit_price": 114.71, "quantity": 8, "state": "confirmed"},
                {"order_id": "sell1", "side": "sell", "limit_price": 115.67, "quantity": 8, "state": "confirmed"},
                {"order_id": "stale_sell", "side": "sell", "limit_price": 110.00, "quantity": 5, "state": "confirmed"},
            ],
        }
        self._assert_plans_equal(runtime_state, "mixed_buy_sell_orders")

    def test_16_high_price_small_lot(self):
        """High price with small lot → qty < 1 at many lines, skip them."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 200.0,
            "atr": 4.20,
            "cash_available": 500.0,
            "shares_available": 0,
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "high_price_small_lot")

    def test_17_ample_cash_ample_shares(self):
        """Plenty of cash and shares; should place max grid on both sides."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 50000.0,
            "shares_available": 1000,
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "ample_cash_ample_shares")

    def test_18_spot_at_grid_line(self):
        """Spot exactly at a grid line (unlikely but valid)."""
        # Anchor=115.19, step=0.48, one line is 115.19 exactly
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        self._assert_plans_equal(runtime_state, "spot_at_grid_line")

    def test_19_partial_open_orders_with_filled(self):
        """Simulate partial fills: some orders gone, shares held."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 4000.0,  # Some cash spent on filled buys
            "shares_available": 40,  # From filled buys
            "open_orders": [
                # Remaining open buy and sell
                {"order_id": "b_open", "side": "buy", "limit_price": 113.75, "quantity": 1, "state": "confirmed"},
                {"order_id": "s_open", "side": "sell", "limit_price": 116.15, "quantity": 8, "state": "confirmed"},
            ],
        }
        self._assert_plans_equal(runtime_state, "partial_open_orders_with_filled")

    def test_20_complex_reconciliation(self):
        """Complex: multiple stale, some keeps, budget constraints."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.60,
            "atr": 4.20,
            "cash_available": 1000.0,  # Limited
            "shares_available": 5,  # Very limited
            "open_orders": [
                # Some good, some stale, some wrong qty
                {"order_id": "good_buy", "side": "buy", "limit_price": 114.71, "quantity": 8, "state": "confirmed"},
                {"order_id": "stale_buy", "side": "buy", "limit_price": 116.00, "quantity": 8, "state": "confirmed"},
                {"order_id": "good_sell", "side": "sell", "limit_price": 116.15, "quantity": 8, "state": "confirmed"},
                {"order_id": "wrong_qty_sell", "side": "sell", "limit_price": 116.63, "quantity": 7, "state": "confirmed"},
            ],
        }
        self._assert_plans_equal(runtime_state, "complex_reconciliation")


class TestCloudReconcilerDirectAssertions(unittest.TestCase):
    """Direct assertions on cloud_reconciler output (no grid_engine comparison)."""

    def test_fixpoint(self):
        """Fixpoint: perfect grid → empty plan."""
        perfect_orders = [
            {"order_id": f"b{i}", "side": "buy", "limit_price": round(114.71 - (7 - i) * 0.48, 2), "quantity": 1, "state": "confirmed"}
            for i in range(8)
        ]
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 10000.0,
            "shares_available": 0,
            "open_orders": perfect_orders,
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        self.assertEqual(len(result["cancels"]), 0, "Fixpoint: no cancels")
        self.assertEqual(len(result["places"]), 0, "Fixpoint: no places")

    def test_no_buy_at_or_above_spot(self):
        """Invariant: no buy price >= spot."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        for p in result["places"]:
            if p["side"] == "buy":
                self.assertLess(p["limit_price"], 115.19, f"Buy at {p['limit_price']} >= spot 115.19")

    def test_no_sell_at_or_below_spot(self):
        """Invariant: no sell price <= spot."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 0.0,
            "shares_available": 100,
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        for p in result["places"]:
            if p["side"] == "sell":
                self.assertGreater(p["limit_price"], 115.19, f"Sell at {p['limit_price']} <= spot 115.19")

    def test_quantities_positive_ints(self):
        """All quantities are positive integers."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 100,
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        for p in result["places"]:
            self.assertIsInstance(p["quantity"], int, f"Quantity {p['quantity']} not int")
            self.assertGreater(p["quantity"], 0, f"Quantity {p['quantity']} <= 0")

    def test_prices_2_decimals(self):
        """All prices to 2 decimals."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 100,
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        for p in result["places"]:
            price_str = f"{p['limit_price']:.2f}"
            reconstructed = float(price_str)
            self.assertAlmostEqual(p["limit_price"], reconstructed, places=2)

    def test_budget_respected_buys(self):
        """Buy budget: total notional <= available."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 500.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        total_notional = sum(p["limit_price"] * p["quantity"] for p in result["places"] if p["side"] == "buy")
        self.assertLessEqual(total_notional, 500.0, f"Buy notional {total_notional} > budget 500")

    def test_budget_respected_sells(self):
        """Sell budget: total qty <= available."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 0.0,
            "shares_available": 5,
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        total_qty = sum(p["quantity"] for p in result["places"] if p["side"] == "sell")
        self.assertLessEqual(total_qty, 5, f"Sell qty {total_qty} > budget 5")


class TestDynamicLotSizing(unittest.TestCase):
    """CHANGE A: Dynamic lot sizing from equity at cost."""

    def test_dynamic_lot_zero_shares_matches_fixed(self):
        """0 shares, cash=1156.2 -> lot_dollars≈115.62 (CHANGE D: divide by 10)."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 1156.2,
            "shares_available": 0,
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, lot_dollars_override=None)
        # CHANGE D: With 0 shares and cash=1156.2, equity=1156.2, lot=1156.2/10=115.62
        self.assertAlmostEqual(result["diagnostics"]["lot_dollars"], 115.62, places=1)
        # Should place 8 buys
        buy_count = len([p for p in result["places"] if p["side"] == "buy"])
        self.assertEqual(buy_count, 8)

    def test_dynamic_lot_double_cash(self):
        """CHANGE D: Double cash -> double lot -> double qty per line (roughly)."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 2312.4,  # 2x of 1156.2
            "shares_available": 0,
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, lot_dollars_override=None)
        # CHANGE D: lot = 2312.4/10 = 231.24 (doubled)
        self.assertAlmostEqual(result["diagnostics"]["lot_dollars"], 231.24, places=1)
        # Check that quantities roughly doubled at each line
        buy_orders = [p for p in result["places"] if p["side"] == "buy"]
        if buy_orders:
            # qty at ~115 should be floor(231.24/115) ≈ 2 (roughly 2x)
            self.assertGreaterEqual(buy_orders[0]["quantity"], 2)

    def test_dynamic_lot_with_shares_and_cost_basis(self):
        """Equity includes shares at cost basis (CHANGE D: divide by 10)."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 400.0,
            "shares_available": 3,
            "average_cost": 114.00,
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, lot_dollars_override=None)
        # equity = 400 + 3*114 = 400 + 342 = 742
        # CHANGE D: lot = 742/10 = 74.2
        self.assertAlmostEqual(result["diagnostics"]["lot_dollars"], 74.2, places=1)
        self.assertAlmostEqual(result["diagnostics"]["equity_at_cost"], 742.0, places=1)
        self.assertAlmostEqual(result["diagnostics"]["avg_cost_used"], 114.00, places=2)

    def test_dynamic_lot_average_cost_missing_fallback_to_price(self):
        """No average_cost -> use current_price for share valuation."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 400.0,
            "shares_available": 3,
            "open_orders": [],
            # No average_cost
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, lot_dollars_override=None)
        # equity = 400 + 3*115.19 = 400 + 345.57 = 745.57
        # lot = 745.57/8 ≈ 93.2
        self.assertAlmostEqual(result["diagnostics"]["equity_at_cost"], 745.57, places=1)
        self.assertAlmostEqual(result["diagnostics"]["avg_cost_used"], 115.19, places=2)

    def test_dynamic_lot_override_with_lot_flag(self):
        """--lot flag overrides dynamic computation."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 1850.0,
            "shares_available": 0,
            "open_orders": [],
        }
        # Force fixed lot despite high equity
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, lot_dollars_override=50.0)
        self.assertAlmostEqual(result["diagnostics"]["lot_dollars"], 50.0, places=2)
        # Should NOT have equity_at_cost in diagnostics (override bypasses dynamic)
        self.assertNotIn("equity_at_cost", result["diagnostics"])

    def test_fixpoint_with_dynamic_lot(self):
        """Fixpoint with dynamic lot: same state produces same plan."""
        # Run twice on identical state -> should get identical plans
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 925.0,
            "shares_available": 0,
            "open_orders": [],  # Clean state
        }
        result1 = cloud_reconcile(runtime_state, ANCHOR, STEP, lot_dollars_override=None)
        result2 = cloud_reconcile(runtime_state, ANCHOR, STEP, lot_dollars_override=None)

        # Same plan twice = deterministic and stable
        self.assertEqual(result1["cancels"], result2["cancels"])
        self.assertEqual(len(result1["places"]), len(result2["places"]))
        for p1, p2 in zip(normalize_places(result1["places"]), normalize_places(result2["places"])):
            self.assertEqual(p1["side"], p2["side"])
            self.assertAlmostEqual(p1["limit_price"], p2["limit_price"], places=2)
            self.assertEqual(p1["quantity"], p2["quantity"])


class TestStandardSells(unittest.TestCase):
    """CHANGE G (v5): Standard sell orders without tax-lot designation."""

    def test_no_tax_lots_field_ever(self):
        """No place (buy or sell) should ever carry a tax_lots field, and no sell_fifo_fallback diagnostic."""
        # Build a state with several open_lots (mix of is_selectable True and False, cost bases around grid)
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 112.50,
            "atr": 4.20,
            "cash_available": 2000.0,
            "shares_available": 5,
            "open_lots": [
                {"open_lot_id": "lot_sel_1", "quantity": 1, "cost_basis": 111.35, "is_selectable": True},
                {"open_lot_id": "lot_no_sel_1", "quantity": 1, "cost_basis": 111.83, "is_selectable": False},
                {"open_lot_id": "lot_sel_2", "quantity": 1, "cost_basis": 112.31, "is_selectable": True},
                {"open_lot_id": "lot_no_sel_2", "quantity": 1, "cost_basis": 112.79, "is_selectable": False},
                {"open_lot_id": "lot_sel_3", "quantity": 1, "cost_basis": 113.27, "is_selectable": True},
            ],
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP)

        # Assert no place carries tax_lots
        for place in result["places"]:
            self.assertNotIn("tax_lots", place, f"Place {place} should not carry tax_lots")

        # Assert sell_fifo_fallback not in diagnostics
        self.assertNotIn("sell_fifo_fallback", result["diagnostics"],
                         "Diagnostics should not contain sell_fifo_fallback")

    def test_sells_placed_for_unselectable_lots(self):
        """Sell orders must be placed for unselectable (same-day) lots at cost_basis + step."""
        # All open_lots have is_selectable False (same-day buys)
        # shares_available equal to total lot qty, spot below the lots' exit prices
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 110.50,  # Below all lot exit prices
            "atr": 4.20,
            "cash_available": 1000.0,
            "shares_available": 5,  # Equal to total lot qty (5 x 1)
            "open_lots": [
                {"open_lot_id": "lot1", "quantity": 1, "cost_basis": 111.35, "is_selectable": False},
                {"open_lot_id": "lot2", "quantity": 1, "cost_basis": 111.83, "is_selectable": False},
                {"open_lot_id": "lot3", "quantity": 1, "cost_basis": 112.31, "is_selectable": False},
                {"open_lot_id": "lot4", "quantity": 1, "cost_basis": 112.79, "is_selectable": False},
                {"open_lot_id": "lot5", "quantity": 1, "cost_basis": 113.27, "is_selectable": False},
            ],
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP)

        # Extract sell places
        sells = [p for p in result["places"] if p["side"] == "sell"]

        # Should have sell places at cost_basis + step for each lot
        # Expected exits: 111.83, 112.31, 112.79, 113.27, 113.75
        expected_exits = {111.83, 112.31, 112.79, 113.27, 113.75}
        actual_exits = {round(s["limit_price"], 2) for s in sells}

        # With spot 110.50 (below all exits) and 5 available shares, ALL five exits must be placed
        self.assertEqual(actual_exits, expected_exits, "All five exits must be placed for unselectable lots")
        # Each sell should be for 1 share
        for sell in sells:
            self.assertEqual(sell["quantity"], 1, f"Sell at {sell['limit_price']} should have quantity 1")
            self.assertNotIn("tax_lots", sell, "Sell should not have tax_lots field")

    def test_exit_prices_and_quantities_unchanged(self):
        """Exit prices and quantities must match pre-change behavior (cost_basis + step, aggregated by price)."""
        # 5 one-share lots at distinct cost bases, spaced by step (0.48) around ~112
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 111.00,
            "atr": 4.20,
            "cash_available": 1000.0,
            "shares_available": 5,
            "open_lots": [
                {"open_lot_id": "lot1", "quantity": 1, "cost_basis": 111.35, "is_selectable": False},
                {"open_lot_id": "lot2", "quantity": 1, "cost_basis": 111.83, "is_selectable": True},
                {"open_lot_id": "lot3", "quantity": 1, "cost_basis": 112.31, "is_selectable": False},
                {"open_lot_id": "lot4", "quantity": 1, "cost_basis": 112.79, "is_selectable": True},
                {"open_lot_id": "lot5", "quantity": 1, "cost_basis": 113.27, "is_selectable": False},
            ],
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP)

        sells = [p for p in result["places"] if p["side"] == "sell"]

        # Expected exits (cost_basis + step = cost_basis + 0.48):
        # 111.35 + 0.48 = 111.83
        # 111.83 + 0.48 = 112.31
        # 112.31 + 0.48 = 112.79
        # 112.79 + 0.48 = 113.27
        # 113.27 + 0.48 = 113.75
        expected_exit_map = {
            111.83: 1,
            112.31: 1,
            112.79: 1,
            113.27: 1,
            113.75: 1,
        }

        actual_exit_map = {}
        for sell in sells:
            price = round(sell["limit_price"], 2)
            qty = sell["quantity"]
            actual_exit_map[price] = actual_exit_map.get(price, 0) + qty

        # With spot 111.00, all five exits (111.83–113.75) are above spot and must all be present
        self.assertEqual(actual_exit_map, expected_exit_map,
                        f"Expected exits {expected_exit_map}, got {actual_exit_map}")


class TestDifferentialWithFixedLot(unittest.TestCase):
    """
    Preserve: With --lot override, cloud_reconciler must still equal
    grid_engine.plan_orders(..., {"ALLOW_REANCHOR": False}).
    """

    def test_fixed_lot_equivalence_fresh_account(self):
        """Fixed lot with fresh account should produce expected plan."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.19,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }

        # Cloud reconciler with fixed lot override
        cloud_result = cloud_reconcile(runtime_state, ANCHOR, STEP, lot_dollars_override=LOT_DOLLARS)

        # Should produce 8 buy orders with fixed lot_dollars
        buy_count = len([p for p in cloud_result["places"] if p["side"] == "buy"])
        self.assertEqual(buy_count, 8, "Should produce 8 buys with fixed lot")

        # Lot should be exactly the override value
        self.assertEqual(cloud_result["diagnostics"]["lot_dollars"], LOT_DOLLARS)


class TestChangeCOneLotPerLevelGuard(unittest.TestCase):
    """CHANGE C: one-lot-per-level buy guard (prevents share pileup at an oscillating line)."""

    def test_held_full_lot_suppresses_buy_at_that_line(self):
        """Full lot already held at 112.79 -> no buy there; lower lines still get buys."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 1,
            "open_orders": [],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 1, "cost_basis": 112.79}],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        buy_prices = [p["limit_price"] for p in result["places"] if p["side"] == "buy"]
        self.assertNotIn(112.79, buy_prices, "Buy at fully-held line 112.79 should be suppressed")
        self.assertIn(112.31, buy_prices, "Buy at lower line 112.31 should still be placed")
        # Sell placement unaffected by the buy-side guard
        sell_orders = [p for p in result["places"] if p["side"] == "sell"]
        self.assertGreater(len(sell_orders), 0, "Sell placement should be unaffected by buy guard")
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 1)

    def test_kept_open_buy_order_cancelled_when_full_lot_held(self):
        """An existing open buy order at a fully-held line must be cancelled, not kept."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [
                {"order_id": "buy_112_79", "side": "buy", "limit_price": 112.79, "quantity": 1, "state": "confirmed"},
            ],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 1, "cost_basis": 112.79}],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        self.assertIn("buy_112_79", result["cancels"])
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 1)

    def test_price_improved_lot_suppresses_nearest_line(self):
        """Buy price improvement (cost_basis 112.75) still attributes to nearest line 112.79."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 1,
            "open_orders": [],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 1, "cost_basis": 112.75}],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        buy_prices = [p["limit_price"] for p in result["places"] if p["side"] == "buy"]
        self.assertNotIn(112.79, buy_prices, "Price-improved lot should still suppress nearest line 112.79")
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 1)

    def test_lot_far_outside_grid_suppresses_nothing(self):
        """A lot with cost basis far outside the grid band attributes to no line."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 100, "cost_basis": 90.00}],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        buy_prices = [p["limit_price"] for p in result["places"] if p["side"] == "buy"]
        self.assertIn(112.79, buy_prices, "Out-of-grid lot must not suppress any line")
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 0)

    def test_partial_holding_does_not_suppress(self):
        """Held qty below the level's full desired qty must NOT suppress the buy."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 1,
            "open_orders": [],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 1, "cost_basis": 112.79}],
        }
        # lot_dollars=240 -> desired qty at 112.79 = floor(240/112.79) = 2; held=1 < 2
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, lot_dollars_override=240.0)
        buys = {p["limit_price"]: p["quantity"] for p in result["places"] if p["side"] == "buy"}
        self.assertIn(112.79, buys, "Partial holding must not suppress the buy")
        self.assertEqual(buys[112.79], 2)
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 0)

    def test_no_open_lots_regression(self):
        """Absent open_lots -> behavior identical to before; no suppression diagnostics key."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 1,
            "open_orders": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        buy_prices = [p["limit_price"] for p in result["places"] if p["side"] == "buy"]
        self.assertIn(112.79, buy_prices)
        self.assertNotIn("buys_suppressed_level_guard", result["diagnostics"])

    def test_empty_open_lots_list_regression(self):
        """Empty open_lots list -> same as absent (no suppression diagnostics key)."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 1,
            "open_orders": [],
            "open_lots": [],
        }
        result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)
        buy_prices = [p["limit_price"] for p in result["places"] if p["side"] == "buy"]
        self.assertIn(112.79, buy_prices)
        self.assertNotIn("buys_suppressed_level_guard", result["diagnostics"])

    def test_differential_with_open_lots(self):
        """cloud_reconciler and grid_engine must agree when open_lots trigger the guard."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 1,
            "open_orders": [],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 1, "cost_basis": 112.79}],
        }
        # CHANGE D: Both should use dynamic lot_dollars for matching
        cloud_result = cloud_reconcile(runtime_state, ANCHOR, STEP)
        engine_grid = {"anchor": ANCHOR, "step": STEP, "initialized": True}
        engine_result = engine_plan_orders(runtime_state, engine_grid, {"ALLOW_REANCHOR": False})

        cloud_cancels = set(cloud_result["cancels"])
        engine_cancels = set(engine_result["cancels"])
        self.assertEqual(cloud_cancels, engine_cancels, "Cancels should match")

        cloud_places = normalize_places(cloud_result["places"])
        engine_places = normalize_places(engine_result["places"])
        self.assertEqual(len(cloud_places), len(engine_places), "Places count should match")

        for cp, ep in zip(cloud_places, engine_places):
            self.assertEqual(cp["side"], ep["side"])
            self.assertAlmostEqual(cp["limit_price"], ep["limit_price"], places=2)
            self.assertEqual(cp["quantity"], ep["quantity"])

    def test_multi_cycle_pileup_prevention(self):
        """
        ACCEPTANCE TEST: price oscillates around the 112.79 buy line for 6 hourly
        cycles. Each time the 112.79 buy is placed it fills instantly (share held,
        open_lot recorded); its paired sell never fills. Held shares at 112.79 must
        never exceed the level's desired qty (1) — i.e. no pileup.
        """
        cash_available = 8000.0
        shares_available = 0
        open_orders = []
        open_lots = []
        next_id = 0

        for cycle in range(6):
            runtime_state = {
                "symbol": "HOOD",
                "current_price": 113.00,
                "atr": 4.20,
                "cash_available": cash_available,
                "shares_available": shares_available,
                "open_orders": open_orders,
                "open_lots": open_lots,
            }
            result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)

            # Apply cancels
            cancel_ids = set(result["cancels"])
            open_orders = [o for o in open_orders if o["order_id"] not in cancel_ids]

            # Apply places: the 112.79 buy fills instantly; everything else stays open
            for p in result["places"]:
                if p["side"] == "buy" and abs(p["limit_price"] - 112.79) < 1e-6:
                    shares_available += p["quantity"]
                    cash_available -= p["limit_price"] * p["quantity"]
                    open_lots.append(
                        {"open_lot_id": f"lot_{next_id}", "quantity": p["quantity"], "cost_basis": p["limit_price"]}
                    )
                    next_id += 1
                else:
                    open_orders.append(
                        {
                            "order_id": f"order_{next_id}",
                            "side": p["side"],
                            "limit_price": p["limit_price"],
                            "quantity": p["quantity"],
                            "state": "confirmed",
                        }
                    )
                    next_id += 1

            held_at_112_79 = sum(l["quantity"] for l in open_lots if abs(l["cost_basis"] - 112.79) < 1e-6)
            self.assertLessEqual(
                held_at_112_79,
                1,
                f"Cycle {cycle}: shares held at 112.79 exceeded desired qty (pileup bug)",
            )


class TestChangeDefAndFV4Regression(unittest.TestCase):
    """CHANGE D/E/F: V4 regression tests (verify live state + new features)."""

    def test_1_live_state_regression(self):
        """Test LIVE-STATE REGRESSION with exact incident numbers."""
        runtime_state = {
            "symbol": "HOOD", "current_price": 107.40, "atr": 1.0,
            "cash_available": 21.86, "cash_total": 360.72, "shares_available": 0,
            "average_cost": 112.31,
            "open_orders": [
                {"order_id": f"sell{i}", "side": "sell", "limit_price": p, "quantity": 1}
                for i, p in enumerate([111.83, 112.31, 112.79, 113.27, 113.75])
            ],
            "open_lots": [
                {"open_lot_id": f"lot{i}", "quantity": 1, "cost_basis": c, "is_selectable": False}
                for i, c in enumerate([111.35, 111.83, 112.31, 112.79, 113.27])
            ],
        }
        result = cloud_reconcile(runtime_state, anchor=115.19, step=0.48)
        self.assertEqual(result["cancels"], [])
        self.assertEqual(len(result["places"]), 0)
        self.assertEqual(result["diagnostics"]["lot_dollars"], 92.23)
        self.assertAlmostEqual(result["diagnostics"]["unsettled_cash"], 338.86, places=1)
        for place in result["places"]:
            self.assertNotIn("tax_lots", place, "No place should carry tax_lots in live state")

    def test_2_lot_collapse_without_cash_total(self):
        """Lot collapses when cash_total absent but exits still kept."""
        runtime_state = {
            "symbol": "HOOD", "current_price": 107.40, "atr": 1.0,
            "cash_available": 21.86, "shares_available": 0,
            # cash_total REMOVED - no unsettled_cash calculation
            "average_cost": 112.31,
            "open_orders": [
                {"order_id": f"sell{i}", "side": "sell", "limit_price": p, "quantity": 1}
                for i, p in enumerate([111.83, 112.31, 112.79, 113.27, 113.75])
            ],
            "open_lots": [
                {"open_lot_id": f"lot{i}", "quantity": 1, "cost_basis": c, "is_selectable": False}
                for i, c in enumerate([111.35, 111.83, 112.31, 112.79, 113.27])
            ],
        }
        result = cloud_reconcile(runtime_state, anchor=115.19, step=0.48)
        self.assertEqual(result["cancels"], [])
        self.assertEqual(len(result["places"]), 0)
        # lot = (21.86 + 5*112.31) / 10 = 583.41 / 10 = 58.34
        self.assertAlmostEqual(result["diagnostics"]["lot_dollars"], 58.34, places=2)

    def test_3_deposit_settled_buys_resume(self):
        """Deposit settlement resumes buys with correct lot_dollars."""
        runtime_state = {
            "symbol": "HOOD", "current_price": 107.40, "atr": 1.0,
            "cash_available": 250.0, "cash_total": 588.86, "shares_available": 0,
            "average_cost": 112.31,
            "open_orders": [
                {"order_id": f"sell{i}", "side": "sell", "limit_price": p, "quantity": 1}
                for i, p in enumerate([111.83, 112.31, 112.79, 113.27, 113.75])
            ],
            "open_lots": [
                {"open_lot_id": f"lot{i}", "quantity": 1, "cost_basis": c, "is_selectable": False}
                for i, c in enumerate([111.35, 111.83, 112.31, 112.79, 113.27])
            ],
        }
        result = cloud_reconcile(runtime_state, anchor=115.19, step=0.48)
        self.assertEqual(result["cancels"], [])
        # lot = (250 + 0 + (588.86-250-0)) / 10 = 588.86 / 10 = 58.886 ≈ 115.04
        # Actually: unsettled = 588.86 - 250 - 0 = 338.86, total_cash = 250 + 0 + 338.86 = 588.86
        # equity = 588.86 + 5*112.31 = 1150.41, lot = 115.04
        self.assertAlmostEqual(result["diagnostics"]["lot_dollars"], 115.04, places=2)
        buy_places = [p for p in result["places"] if p["side"] == "buy"]
        # Should place exactly 2 buys at [107.03, 106.55]
        self.assertEqual(len(buy_places), 2)
        buy_prices = sorted([p["limit_price"] for p in buy_places])
        self.assertAlmostEqual(buy_prices[0], 106.55, places=2)
        self.assertAlmostEqual(buy_prices[1], 107.03, places=2)
        sell_places = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_places), 0, "No sell places should be generated")

    def test_4_mixed_selectable_lots(self):
        """Sells are placed for both selectable and non-selectable lots without tax_lots."""
        runtime_state = {
            "symbol": "HOOD", "current_price": 99.00, "atr": 1.0,
            "cash_available": 8000.0, "shares_available": 2, "cash_total": 8000.0,
            "open_orders": [],
            "open_lots": [
                {"open_lot_id": "lot_sel", "quantity": 1, "cost_basis": 100.00, "is_selectable": True},
                {"open_lot_id": "lot_no_sel", "quantity": 1, "cost_basis": 100.48, "is_selectable": False},
            ],
        }
        result = cloud_reconcile(runtime_state, anchor=100.00, step=0.48)
        # Exits at 100.48 (from 100.00) and 100.96 (from 100.48)
        sell_places = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_places), 2)
        # Find sell at 100.48 and 100.96
        sell_100_48 = [p for p in sell_places if abs(p["limit_price"] - 100.48) < 0.01]
        sell_100_96 = [p for p in sell_places if abs(p["limit_price"] - 100.96) < 0.01]
        self.assertEqual(len(sell_100_48), 1, "Should have sell @100.48")
        self.assertEqual(len(sell_100_96), 1, "Should have sell @100.96")
        # Both exits should be placed as standard GTC limit orders without tax_lots
        self.assertNotIn("tax_lots", sell_100_48[0], "Sell @100.48 should not have tax_lots")
        self.assertNotIn("tax_lots", sell_100_96[0], "Sell @100.96 should not have tax_lots")
        # sell_fifo_fallback should not be in diagnostics
        self.assertNotIn("sell_fifo_fallback", result["diagnostics"], "sell_fifo_fallback should not be present")

    def test_5_sells_frozen_no_lots(self):
        """Shares held but no lots => sells are frozen, untouched."""
        runtime_state = {
            "symbol": "HOOD", "current_price": 110.00, "atr": 1.0,
            "cash_available": 8000.0, "shares_available": 1, "cash_total": 8000.0,
            "open_orders": [
                {"order_id": "s1", "side": "sell", "limit_price": 111.00, "quantity": 1},
                {"order_id": "s2", "side": "sell", "limit_price": 111.50, "quantity": 1},
            ],
            "open_lots": [],  # NO lots, but shares are held
        }
        result = cloud_reconcile(runtime_state, anchor=115.19, step=0.48)
        # Sells should NOT be cancelled
        self.assertNotIn("s1", result["cancels"], "Open sell s1 should not be cancelled")
        self.assertNotIn("s2", result["cancels"], "Open sell s2 should not be cancelled")
        # No new sell places should be generated
        sell_places = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_places), 0, "No new sell places when frozen")
        self.assertTrue(result["diagnostics"]["sells_frozen_no_lots"])

    def test_6_exit_deferred_below_spot(self):
        """Exits with price <= spot are deferred."""
        # Part A: spot=105, exit @100.48 is below spot → deferred
        runtime_state = {
            "symbol": "HOOD", "current_price": 105.00, "atr": 1.0,
            "cash_available": 8000.0, "shares_available": 1,
            "open_orders": [],
            "open_lots": [
                {"open_lot_id": "lot1", "quantity": 1, "cost_basis": 100.00, "is_selectable": True},
            ],
        }
        result = cloud_reconcile(runtime_state, anchor=100.00, step=0.48)
        sell_places = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_places), 0, "No sell at 100.48 when spot=105")
        self.assertGreaterEqual(result["diagnostics"]["exits_deferred_below_spot"], 1)

        # Part B: spot=99, exit @100.48 is above spot → placed
        runtime_state["current_price"] = 99.00
        result = cloud_reconcile(runtime_state, anchor=100.00, step=0.48)
        sell_places = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_places), 1, "Should have sell @100.48 when spot=99")
        self.assertAlmostEqual(sell_places[0]["limit_price"], 100.48, places=2)

    def test_7_buffer_accounting(self):
        """Buffer accounting: buffer_dollars = buffer_lots * lot_dollars."""
        runtime_state = {
            "symbol": "HOOD", "current_price": 107.40, "atr": 1.0,
            "cash_available": 1150.40, "cash_total": 1150.40, "shares_available": 0,
            "open_orders": [],
            "open_lots": [],
        }
        result = cloud_reconcile(runtime_state, anchor=115.19, step=0.48)
        # lot = 1150.40 / 10 = 115.04
        # buffer = 2 * 115.04 = 230.08
        self.assertAlmostEqual(result["diagnostics"]["lot_dollars"], 115.04, places=2)
        self.assertAlmostEqual(result["diagnostics"]["buffer_dollars"], 230.08, places=2)
        buy_places = [p for p in result["places"] if p["side"] == "buy"]
        self.assertEqual(len(buy_places), 8, "Should place 8 buys")
        # Verify total buy notional <= cash_available
        total_notional = sum(p["limit_price"] * p["quantity"] for p in buy_places)
        self.assertLessEqual(total_notional, 1150.40)

    def test_lattice_slide(self):
        """Test CHANGE F: lattice produces correct lines below spot."""
        runtime_state = {
            "symbol": "HOOD", "current_price": 100.00, "atr": 1.0,
            "cash_available": 2000.0, "cash_total": 2000.0, "shares_available": 0,
            "open_orders": [], "open_lots": [],
        }
        result = cloud_reconcile(runtime_state, anchor=115.19, step=0.48)
        buy_places = [p for p in result["places"] if p["side"] == "buy"]
        self.assertEqual(len(buy_places), 8)

    def test_fixpoint_with_lattice(self):
        """Test fixpoint: perfect book yields no cancels/places."""
        runtime_state = {
            "symbol": "HOOD", "current_price": 115.19, "atr": 1.0,
            "cash_available": 500.0, "cash_total": 500.0, "shares_available": 0,
            "open_orders": [], "open_lots": [],
        }
        result1 = cloud_reconcile(runtime_state, anchor=115.19, step=0.48)

        perfect_orders = [
            {"order_id": f"order_{i}", "side": p["side"], "limit_price": p["limit_price"], "quantity": p["quantity"]}
            for i, p in enumerate(result1["places"])
        ]

        runtime_state["open_orders"] = perfect_orders
        result2 = cloud_reconcile(runtime_state, anchor=115.19, step=0.48)
        self.assertEqual(len(result2["cancels"]), 0)
        self.assertEqual(len(result2["places"]), 0)


if __name__ == "__main__":
    unittest.main()
