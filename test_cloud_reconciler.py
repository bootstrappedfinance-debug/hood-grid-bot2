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
        # Cloud reconciler
        cloud_result = cloud_reconcile(runtime_state, ANCHOR, STEP, LOT_DOLLARS)

        # Grid engine in fixed mode
        engine_grid = {"anchor": ANCHOR, "step": STEP, "lot_dollars": LOT_DOLLARS, "initialized": True}
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


if __name__ == "__main__":
    unittest.main()
