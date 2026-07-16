#!/usr/bin/env python3
"""
Unit tests for grid_engine.py.

Comprehensive coverage of core functions, invariants, and edge cases.
Uses stdlib unittest (no pytest).
"""

import unittest
import json
import tempfile
import os
from pathlib import Path

from grid_engine import (
    initialize_grid,
    needs_reanchor,
    grid_lines,
    plan_orders,
)


class TestInitializeGrid(unittest.TestCase):
    """Tests for initialize_grid()."""

    def test_basic_initialization(self):
        """Test basic grid initialization."""
        result = initialize_grid(current_price=115.54, atr=4.20, cash_available=8000)
        self.assertTrue(result["initialized"])
        self.assertAlmostEqual(result["anchor"], 115.54, places=2)
        self.assertAlmostEqual(result["lot_dollars"], 1000.0, places=2)  # 8000 / 8
        # step = max(4.20 * 0.25, 0.25) = max(1.05, 0.25) = 1.05
        self.assertAlmostEqual(result["step"], 1.05, places=2)

    def test_atr_below_min_step(self):
        """Test that MIN_STEP floor is applied."""
        result = initialize_grid(current_price=100.0, atr=0.5, cash_available=1000)
        # step = max(0.5 * 0.25, 0.25) = max(0.125, 0.25) = 0.25
        self.assertAlmostEqual(result["step"], 0.25, places=2)

    def test_lot_dollars_calculation(self):
        """Test lot_dollars = cash / NUM_LEVELS (8)."""
        result = initialize_grid(current_price=100.0, atr=1.0, cash_available=2400)
        # 2400 / 8 = 300
        self.assertAlmostEqual(result["lot_dollars"], 300.0, places=2)

    def test_anchor_rounding(self):
        """Test that anchor is rounded to 2 decimals."""
        result = initialize_grid(current_price=115.546, atr=1.0, cash_available=1000)
        self.assertAlmostEqual(result["anchor"], 115.55, places=2)

    def test_step_rounding_to_tick(self):
        """Test that step is rounded to TICK (0.01)."""
        # ATR = 4.44 * 0.25 = 1.11 (already a tick multiple)
        result = initialize_grid(current_price=100.0, atr=4.44, cash_available=1000)
        # step = 1.11, which is a tick multiple
        self.assertAlmostEqual(result["step"], 1.11, places=2)

        # ATR = 4.45 * 0.25 = 1.1125, should round to 1.11 or 1.12
        result2 = initialize_grid(current_price=100.0, atr=4.45, cash_available=1000)
        step = result2["step"]
        # Should be a valid tick (multiple of 0.01)
        self.assertAlmostEqual(step % 0.01, 0.0, places=3)


class TestNeedsReanchor(unittest.TestCase):
    """Tests for needs_reanchor()."""

    def test_price_within_band(self):
        """Price within band should not trigger reanchor."""
        grid_state = {
            "anchor": 115.0,
            "step": 1.0,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Band: 115 +/- 8*1 = [107, 123]
        self.assertFalse(needs_reanchor(grid_state, 115.0))
        self.assertFalse(needs_reanchor(grid_state, 107.0))
        self.assertFalse(needs_reanchor(grid_state, 123.0))

    def test_price_above_upper_band(self):
        """Price above upper band should trigger reanchor."""
        grid_state = {
            "anchor": 115.0,
            "step": 1.0,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Upper band: 115 + 8*1 = 123
        self.assertTrue(needs_reanchor(grid_state, 123.01))
        self.assertTrue(needs_reanchor(grid_state, 130.0))

    def test_price_below_lower_band(self):
        """Price below lower band should trigger reanchor."""
        grid_state = {
            "anchor": 115.0,
            "step": 1.0,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Lower band: 115 - 8*1 = 107
        self.assertTrue(needs_reanchor(grid_state, 106.99))
        self.assertTrue(needs_reanchor(grid_state, 100.0))

    def test_uninitialized_grid(self):
        """Uninitialized grid should not trigger reanchor."""
        grid_state = {"initialized": False}
        self.assertFalse(needs_reanchor(grid_state, 115.0))
        self.assertFalse(needs_reanchor({}, 115.0))


class TestGridLines(unittest.TestCase):
    """Tests for grid_lines()."""

    def test_line_count(self):
        """Should return 17 lines (8 below, anchor, 8 above)."""
        grid_state = {
            "anchor": 115.0,
            "step": 1.0,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        lines = grid_lines(grid_state)
        self.assertEqual(len(lines), 17)

    def test_line_spacing(self):
        """Lines should be spaced exactly 'step' apart."""
        grid_state = {
            "anchor": 115.0,
            "step": 1.0,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        lines = grid_lines(grid_state)
        for i in range(len(lines) - 1):
            expected_diff = 1.0
            actual_diff = lines[i + 1] - lines[i]
            self.assertAlmostEqual(actual_diff, expected_diff, places=2)

    def test_symmetry_around_anchor(self):
        """Grid should be symmetric around anchor."""
        grid_state = {
            "anchor": 115.0,
            "step": 1.0,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        lines = grid_lines(grid_state)
        mid = len(lines) // 2
        # Anchor should be at index 8
        self.assertAlmostEqual(lines[mid], 115.0, places=2)
        # Check symmetry
        for i in range(1, len(lines) // 2):
            below = lines[mid - i]
            above = lines[mid + i]
            expected_diff = 2 * i * 1.0  # 2*i*step
            self.assertAlmostEqual(above - below, expected_diff, places=2)

    def test_rounding(self):
        """All lines should be rounded to 2 decimals."""
        grid_state = {
            "anchor": 115.545,  # will be rounded
            "step": 0.33,  # odd step
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        lines = grid_lines(grid_state)
        for line in lines:
            # Check if line is at most 2 decimal places
            rounded = round(line, 2)
            self.assertAlmostEqual(line, rounded, places=2)


class TestFreshAccountBuys(unittest.TestCase):
    """Test case: fresh account with cash, no shares, no open orders."""

    def test_fresh_account_generates_buys(self):
        """Fresh account should generate 8 buy orders."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        self.assertTrue(result["grid_state"]["initialized"])
        self.assertEqual(len(result["cancels"]), 0)
        # Should have 8 buy orders
        buy_orders = [p for p in result["places"] if p["side"] == "buy"]
        self.assertEqual(len(buy_orders), 8)
        # No sell orders
        sell_orders = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_orders), 0)

    def test_buy_notional_within_budget(self):
        """Total buy notional should not exceed cash."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        total_notional = sum(
            p["limit_price"] * p["quantity"]
            for p in result["places"]
            if p["side"] == "buy"
        )
        self.assertLessEqual(total_notional, 8000.0)

    def test_all_buys_below_spot(self):
        """All buy orders should be below current price."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        for p in result["places"]:
            if p["side"] == "buy":
                self.assertLess(p["limit_price"], 115.54)


class TestShareOnlySells(unittest.TestCase):
    """Test case: account with more shares than buys, small cash, no open orders."""

    def test_share_only_generates_sells(self):
        """Account with shares should generate sell orders when cash sufficient for sizing."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 100.0,
            "atr": 2.0,
            "cash_available": 2000.0,  # enough for sizing: 2000/8 = 250 lot_dollars
            "shares_available": 100,  # plenty of shares to sell
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        # Should have both buy and sell orders (balanced grid at init)
        # With 100 shares available, at least some sells should fit in budget
        sell_orders = [p for p in result["places"] if p["side"] == "sell"]
        buy_orders = [p for p in result["places"] if p["side"] == "buy"]
        # Should have some orders
        self.assertGreater(len(sell_orders) + len(buy_orders), 0)
        # With good cash and shares, should have roughly balanced grid
        # (8 buys expected, up to 8 sells within share budget)
        total_sell_qty = sum(p["quantity"] for p in sell_orders)
        self.assertLessEqual(total_sell_qty, 100)

    def test_sell_qty_within_shares(self):
        """Total sell quantity should not exceed shares available."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.20,
            "cash_available": 100.0,
            "shares_available": 50,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        total_qty = sum(p["quantity"] for p in result["places"] if p["side"] == "sell")
        self.assertLessEqual(total_qty, 50)

    def test_all_sells_above_spot(self):
        """All sell orders should be above current price."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.20,
            "cash_available": 0.0,
            "shares_available": 100,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        for p in result["places"]:
            if p["side"] == "sell":
                self.assertGreater(p["limit_price"], 115.54)


class TestFixpointIdempotence(unittest.TestCase):
    """CRITICAL: Test fixpoint idempotence."""

    def test_perfect_grid_no_thrash(self):
        """If open orders perfectly match desired grid, no cancels or places."""
        # First run: establish grid
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.00,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result1 = plan_orders(runtime_state, {})
        grid_state = result1["grid_state"]

        # Now simulate: open orders match the desired grid exactly
        perfect_orders = []
        for p in result1["places"]:
            perfect_orders.append(
                {
                    "order_id": f"order_{len(perfect_orders)}",
                    "side": p["side"],
                    "limit_price": p["limit_price"],
                    "quantity": p["quantity"],
                    "state": "confirmed",
                }
            )

        # Second run: with perfect orders in place
        runtime_state["open_orders"] = perfect_orders
        result2 = plan_orders(runtime_state, grid_state)

        # Should have no cancels and no places
        self.assertEqual(len(result2["cancels"]), 0, "Should have no cancels")
        self.assertEqual(len(result2["places"]), 0, "Should have no places")


class TestStaleOrderCancellation(unittest.TestCase):
    """Test that stale orders are cancelled and replaced correctly."""

    def test_buy_above_spot_cancelled(self):
        """A buy order above spot should be cancelled."""
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.00,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [
                {
                    "order_id": "bad_buy",
                    "side": "buy",
                    "limit_price": 116.00,  # ABOVE spot, wrong side
                    "quantity": 8,
                    "state": "confirmed",
                }
            ],
        }
        result = plan_orders(runtime_state, grid_state)
        self.assertIn("bad_buy", result["cancels"])


class TestFilledBuyRebalance(unittest.TestCase):
    """Test swing capture: filled buy, spot drops, sell placed at that line."""

    def test_filled_buy_swing_capture(self):
        """When a buy fills and spot drops below, place sell at that line."""
        # Initial: grid established with buys
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Spot dropped slightly
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 114.00,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 8,  # filled buy at 115 line → 8 shares
            "open_orders": [],  # the buy at 115 is gone (filled)
        }
        result = plan_orders(runtime_state, grid_state)
        # Should now place a sell at the line where shares came from
        sell_orders = [p for p in result["places"] if p["side"] == "sell"]
        self.assertGreater(len(sell_orders), 0, "Should place sell orders for swing capture")


class TestCashBudgetCap(unittest.TestCase):
    """Test that small cash budget limits buy orders."""

    def test_small_cash_limits_buys(self):
        """Small cash should limit number of buy orders."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 100.0,
            "atr": 4.00,
            "cash_available": 500.0,  # very small
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        total_notional = sum(
            p["limit_price"] * p["quantity"]
            for p in result["places"]
            if p["side"] == "buy"
        )
        # Should not exceed cash budget
        self.assertLessEqual(total_notional, 500.0)
        # Should not be 8 buy orders (limited by budget)
        buy_count = len([p for p in result["places"] if p["side"] == "buy"])
        self.assertLess(buy_count, 8)


class TestShareBudgetCap(unittest.TestCase):
    """Test that small share budget limits sell orders."""

    def test_small_shares_limits_sells(self):
        """Small share count should limit number of sell orders."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.00,
            "cash_available": 0.0,
            "shares_available": 5,  # very small
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        total_qty = sum(p["quantity"] for p in result["places"] if p["side"] == "sell")
        self.assertLessEqual(total_qty, 5)
        # Should not place many sells
        sell_count = len([p for p in result["places"] if p["side"] == "sell"])
        self.assertLess(sell_count, 8)


class TestReanchor(unittest.TestCase):
    """Test reanchoring when price drifts beyond band."""

    def test_reanchor_above(self):
        """Spot far above band should trigger reanchor."""
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Upper band: 115 + 8*1 = 123, spot = 130 → reanchor
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 130.00,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, grid_state)
        self.assertTrue(result["diagnostics"]["reanchored"])
        self.assertAlmostEqual(result["grid_state"]["anchor"], 130.00, places=2)
        # lot_dollars should be preserved
        self.assertAlmostEqual(result["grid_state"]["lot_dollars"], 1000.0, places=2)

    def test_reanchor_below(self):
        """Spot far below band should trigger reanchor."""
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Lower band: 115 - 8*1 = 107, spot = 100 → reanchor
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 100.00,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, grid_state)
        self.assertTrue(result["diagnostics"]["reanchored"])
        self.assertAlmostEqual(result["grid_state"]["anchor"], 100.00, places=2)
        # lot_dollars should be preserved
        self.assertAlmostEqual(result["grid_state"]["lot_dollars"], 1000.0, places=2)


class TestQtySkipWhenTooSmall(unittest.TestCase):
    """Test that lines with qty < 1 are skipped."""

    def test_small_lot_skips_high_price_lines(self):
        """If lot_dollars < line_price, no order placed."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 500.0,  # very high price
            "atr": 40.0,
            "cash_available": 100.0,  # tiny
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        # lot_dollars = 100 / 8 = 12.5, price around 500 → qty < 1
        # Should have very few or no orders
        self.assertLess(len(result["places"]), 8)


class TestRounding(unittest.TestCase):
    """Test that prices and quantities are properly rounded."""

    def test_all_prices_2_decimals(self):
        """All place prices should be to 2 decimals."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.546,
            "atr": 4.207,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        for p in result["places"]:
            # Check if price has at most 2 decimal places
            price_str = f"{p['limit_price']:.2f}"
            reconstructed = float(price_str)
            self.assertAlmostEqual(p["limit_price"], reconstructed, places=2)

    def test_all_quantities_ints(self):
        """All quantities should be positive integers."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 50,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        for p in result["places"]:
            self.assertIsInstance(p["quantity"], int)
            self.assertGreater(p["quantity"], 0)


class TestBugFix1OffAnchorBuyOrdering(unittest.TestCase):
    """BUG FIX 1: buy_lines must be NEAREST 8, not FARTHEST 8 (prevents thrashing)."""

    def test_off_anchor_buy_ordering(self):
        """Grid anchor=115, step=1, spot=115.60 → nearest 8 buys, NOT farthest."""
        # Establish grid at anchor 115.00
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Current price is 115.60 (above anchor but within band: 115±8)
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.60,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, grid_state)

        # Extract buy prices
        buy_prices = sorted(
            [p["limit_price"] for p in result["places"] if p["side"] == "buy"]
        )

        # Should include nearest line (115.00) but NOT farthest (107.00)
        self.assertIn(115.00, buy_prices, "Nearest line (115.00) should be included")
        self.assertNotIn(
            107.00, buy_prices, "Farthest line (107.00) should NOT be included"
        )
        # Should have 8 buys
        self.assertEqual(len(buy_prices), 8)
        # Expected: 108, 109, 110, 111, 112, 113, 114, 115 (nearest-first ordering)
        expected = [108.0, 109.0, 110.0, 111.0, 112.0, 113.0, 114.0, 115.0]
        self.assertEqual(buy_prices, expected)

    def test_off_anchor_no_thrash(self):
        """If open orders match the correct nearest grid, no cancels/places."""
        # Grid at anchor 115.00
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Current price at 115.60; calculate desired qtys
        # For each line: qty = floor(1000 / line)
        # e.g. 115.00 → floor(1000/115) = 8, 114.00 → floor(1000/114) = 8, etc.
        desired_orders = [
            {"order_id": "b1", "side": "buy", "limit_price": 108.0, "quantity": 9},
            {"order_id": "b2", "side": "buy", "limit_price": 109.0, "quantity": 9},
            {"order_id": "b3", "side": "buy", "limit_price": 110.0, "quantity": 9},
            {"order_id": "b4", "side": "buy", "limit_price": 111.0, "quantity": 9},
            {"order_id": "b5", "side": "buy", "limit_price": 112.0, "quantity": 8},
            {"order_id": "b6", "side": "buy", "limit_price": 113.0, "quantity": 8},
            {"order_id": "b7", "side": "buy", "limit_price": 114.0, "quantity": 8},
            {"order_id": "b8", "side": "buy", "limit_price": 115.0, "quantity": 8},
        ]
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.60,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": desired_orders,
        }
        result = plan_orders(runtime_state, grid_state)

        # Should have no cancels and no places (fixpoint)
        self.assertEqual(len(result["cancels"]), 0, "Should have no cancels")
        self.assertEqual(len(result["places"]), 0, "Should have no places")


class TestBugFix2ZeroCashGuard(unittest.TestCase):
    """BUG FIX 2: Never initialize grid when cash=0 (prevents lot_dollars lock-in)."""

    def test_zero_cash_init_guard(self):
        """Uninitialized grid + cash=0 → empty plan, grid stays uninitialized."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.20,
            "cash_available": 0.0,  # NO CASH
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})

        # Should return empty plan
        self.assertEqual(len(result["cancels"]), 0)
        self.assertEqual(len(result["places"]), 0)

        # Grid should NOT be initialized (lot_dollars not set)
        self.assertFalse(result["grid_state"].get("initialized", False))

        # Diagnostic flag should be set
        self.assertTrue(result["diagnostics"].get("skipped_no_cash", False))

    def test_zero_cash_init_then_funded(self):
        """Run with cash=0 (stays uninitialized), then with cash>0 (initializes)."""
        # First run: cash=0, should skip init
        result1 = plan_orders(
            {
                "symbol": "HOOD",
                "current_price": 115.54,
                "atr": 4.20,
                "cash_available": 0.0,
                "shares_available": 0,
                "open_orders": [],
            },
            {},
        )
        self.assertFalse(result1["grid_state"].get("initialized", False))
        grid_state_after_zero_cash = result1["grid_state"]

        # Second run: now with cash>0, should initialize
        result2 = plan_orders(
            {
                "symbol": "HOOD",
                "current_price": 115.54,
                "atr": 4.20,
                "cash_available": 8000.0,  # NOW funded
                "shares_available": 0,
                "open_orders": [],
            },
            grid_state_after_zero_cash,
        )
        self.assertTrue(result2["grid_state"].get("initialized", False))
        self.assertTrue(result2["diagnostics"]["initialized_now"])
        self.assertAlmostEqual(result2["grid_state"]["lot_dollars"], 1000.0, places=1)

    def test_self_heal_zero_lot_dollars(self):
        """Grid with lot_dollars=0 but initialized=True, then cash>0 → re-initializes."""
        # Simulate a corrupted grid state from a zero-cash init (shouldn't happen, but defend)
        bad_grid_state = {
            "anchor": 115.0,
            "step": 1.0,
            "lot_dollars": 0.0,  # ZERO!
            "initialized": True,
        }
        result = plan_orders(
            {
                "symbol": "HOOD",
                "current_price": 115.54,
                "atr": 4.20,
                "cash_available": 8000.0,
                "shares_available": 0,
                "open_orders": [],
            },
            bad_grid_state,
        )
        # Should re-initialize
        self.assertTrue(result["diagnostics"]["initialized_now"])
        self.assertGreater(result["grid_state"]["lot_dollars"], 0.0)


class TestCloudStatelessMode(unittest.TestCase):
    """Cloud deployment: ALLOW_REANCHOR=False for fixed geometry (no persistence)."""

    def test_cloud_fixed_grid_spot_outside_band(self):
        """ALLOW_REANCHOR=False, spot outside band → no reanchor, fixed grid reconcile."""
        # Fixed grid (passed each cycle, never reanchored)
        grid_state = {
            "anchor": 115.19,
            "step": 0.48,
            "lot_dollars": 115.62,
            "initialized": True,
        }
        # Spot pushed far above band (115.19 + 8*0.48 = 118.03)
        # spot=125.0 is well above
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 125.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        config = {"ALLOW_REANCHOR": False}
        result = plan_orders(runtime_state, grid_state, config)

        # Should NOT reanchor
        self.assertFalse(result["diagnostics"]["reanchored"])
        # Anchor should stay exactly as provided
        self.assertAlmostEqual(result["grid_state"]["anchor"], 115.19, places=2)
        # Step should stay exactly as provided
        self.assertAlmostEqual(result["grid_state"]["step"], 0.48, places=2)

        # With spot=125 above all grid lines, should place only buys (no sells)
        sell_orders = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_orders), 0, "No sells above grid band")

        # All buys should be strictly below spot
        for p in result["places"]:
            if p["side"] == "buy":
                self.assertLess(
                    p["limit_price"], 125.0, "All buys must be below spot=125"
                )

    def test_cloud_fixed_grid_spot_inside_band(self):
        """ALLOW_REANCHOR=False, spot inside band → fixed grid reconcile, no reanchor."""
        grid_state = {
            "anchor": 115.19,
            "step": 0.48,
            "lot_dollars": 115.62,
            "initialized": True,
        }
        # Spot inside band (115.19 - 8*0.48 = 111.35 to 115.19 + 8*0.48 = 118.03)
        # spot=116.0 is inside
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 116.00,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        config = {"ALLOW_REANCHOR": False}
        result = plan_orders(runtime_state, grid_state, config)

        # Should NOT reanchor
        self.assertFalse(result["diagnostics"]["reanchored"])
        # Anchor unchanged
        self.assertAlmostEqual(result["grid_state"]["anchor"], 115.19, places=2)

        # Should reconcile against the fixed grid normally
        # With buys below 116 and sells above 116
        buy_count = len([p for p in result["places"] if p["side"] == "buy"])
        sell_count = len([p for p in result["places"] if p["side"] == "sell"])
        # Should have both (or at least try to place them)
        total = buy_count + sell_count
        self.assertGreater(total, 0, "Should place some orders against fixed grid")

    def test_cloud_no_grid_provided(self):
        """ALLOW_REANCHOR=False, empty grid_state → empty plan, not_initialized=True."""
        config = {"ALLOW_REANCHOR": False}
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.20,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {}, config)

        # Should return empty plan
        self.assertEqual(len(result["cancels"]), 0)
        self.assertEqual(len(result["places"]), 0)

        # Diagnostic flags
        self.assertTrue(result["diagnostics"]["not_initialized"])
        self.assertFalse(result["grid_state"].get("initialized", False))

    def test_cloud_backward_compat_default_allow_reanchor(self):
        """Default ALLOW_REANCHOR=True: existing reanchor behavior unchanged."""
        # Spot far beyond band should trigger reanchor with default config
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Upper band: 115 + 8*1 = 123, spot = 130
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 130.00,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        # No config passed (or ALLOW_REANCHOR=True by default)
        result = plan_orders(runtime_state, grid_state)

        # Should reanchor (backward compatibility: default behavior)
        self.assertTrue(result["diagnostics"]["reanchored"])
        # Anchor should move to current price
        self.assertAlmostEqual(result["grid_state"]["anchor"], 130.00, places=2)


class TestInvariantSweep(unittest.TestCase):
    """Test invariants across varied scenarios."""

    def test_invariant_no_buy_at_or_above_spot(self):
        """No buy should ever be at or above current_price."""
        scenarios = [
            {"price": 100.0, "atr": 2.0, "cash": 5000, "shares": 0},
            {"price": 115.54, "atr": 4.2, "cash": 8000, "shares": 0},
            {"price": 200.0, "atr": 20.0, "cash": 10000, "shares": 0},
        ]
        for scenario in scenarios:
            runtime_state = {
                "symbol": "HOOD",
                "current_price": scenario["price"],
                "atr": scenario["atr"],
                "cash_available": scenario["cash"],
                "shares_available": scenario["shares"],
                "open_orders": [],
            }
            result = plan_orders(runtime_state, {})
            for p in result["places"]:
                if p["side"] == "buy":
                    self.assertLess(
                        p["limit_price"],
                        scenario["price"],
                        f"Buy at {p['limit_price']} should be < {scenario['price']}",
                    )

    def test_invariant_no_sell_at_or_below_spot(self):
        """No sell should ever be at or below current_price."""
        scenarios = [
            {"price": 100.0, "atr": 2.0, "cash": 0, "shares": 50},
            {"price": 115.54, "atr": 4.2, "cash": 0, "shares": 100},
            {"price": 200.0, "atr": 20.0, "cash": 0, "shares": 100},
        ]
        for scenario in scenarios:
            runtime_state = {
                "symbol": "HOOD",
                "current_price": scenario["price"],
                "atr": scenario["atr"],
                "cash_available": scenario["cash"],
                "shares_available": scenario["shares"],
                "open_orders": [],
            }
            result = plan_orders(runtime_state, {})
            for p in result["places"]:
                if p["side"] == "sell":
                    self.assertGreater(
                        p["limit_price"],
                        scenario["price"],
                        f"Sell at {p['limit_price']} should be > {scenario['price']}",
                    )

    def test_invariant_buy_budget_respected(self):
        """Sum of new buy notional <= available_for_buys."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.2,
            "cash_available": 3000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        total_buy_notional = sum(
            p["limit_price"] * p["quantity"]
            for p in result["places"]
            if p["side"] == "buy"
        )
        self.assertLessEqual(total_buy_notional, 3000.0)

    def test_invariant_sell_budget_respected(self):
        """Sum of new sell qty <= available_for_sells."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.2,
            "cash_available": 0.0,
            "shares_available": 20,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        total_sell_qty = sum(
            p["quantity"] for p in result["places"] if p["side"] == "sell"
        )
        self.assertLessEqual(total_sell_qty, 20)

    def test_invariant_max_one_order_per_line(self):
        """At most one order per grid line."""
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.54,
            "atr": 4.2,
            "cash_available": 8000.0,
            "shares_available": 100,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, {})
        # Collect all order prices
        prices = [p["limit_price"] for p in result["places"]]
        # Check for duplicates
        self.assertEqual(len(prices), len(set(prices)), "Duplicate prices found")


if __name__ == "__main__":
    unittest.main()
