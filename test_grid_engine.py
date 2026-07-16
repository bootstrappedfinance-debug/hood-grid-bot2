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
        # CHANGE D: lot_dollars = cash / (NUM_LEVELS + BUFFER_LOTS) = 8000 / 10 = 800
        self.assertAlmostEqual(result["lot_dollars"], 800.0, places=2)
        # step = max(4.20 * 0.25, 0.25) = max(1.05, 0.25) = 1.05
        self.assertAlmostEqual(result["step"], 1.05, places=2)

    def test_atr_below_min_step(self):
        """Test that MIN_STEP floor is applied."""
        result = initialize_grid(current_price=100.0, atr=0.5, cash_available=1000)
        # step = max(0.5 * 0.25, 0.25) = max(0.125, 0.25) = 0.25
        self.assertAlmostEqual(result["step"], 0.25, places=2)

    def test_lot_dollars_calculation(self):
        """CHANGE D: Test lot_dollars = cash / (NUM_LEVELS + BUFFER_LOTS) = cash / 10."""
        result = initialize_grid(current_price=100.0, atr=1.0, cash_available=2400)
        # 2400 / 10 = 240
        self.assertAlmostEqual(result["lot_dollars"], 240.0, places=2)

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

    def test_always_returns_false(self):
        """CHANGE F: needs_reanchor always returns False (lattice makes reanchoring unnecessary)."""
        # Test with various grid states and prices
        grid_state = {
            "anchor": 115.0,
            "step": 1.0,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # No matter the price, reanchor always false
        self.assertFalse(needs_reanchor(grid_state, 115.0))
        self.assertFalse(needs_reanchor(grid_state, 130.0))  # Far above
        self.assertFalse(needs_reanchor(grid_state, 100.0))  # Far below
        self.assertFalse(needs_reanchor(grid_state, 107.0))
        self.assertFalse(needs_reanchor(grid_state, 123.0))

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
        """If open orders perfectly match desired grid, no cancels or places (fixpoint)."""
        # Grid at anchor 115, step 1, current_price 115
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "initialized": True,
        }
        # Buy lines: [114, 113, 112, 111, 110, 109, 108, 107]
        # With lot_dollars = 8000/10 = 800:
        # qty at each line = floor(800/line) = 7 for all lines
        perfect_orders = [
            {"order_id": "b1", "side": "buy", "limit_price": 114.0, "quantity": 7, "state": "confirmed"},
            {"order_id": "b2", "side": "buy", "limit_price": 113.0, "quantity": 7, "state": "confirmed"},
            {"order_id": "b3", "side": "buy", "limit_price": 112.0, "quantity": 7, "state": "confirmed"},
            {"order_id": "b4", "side": "buy", "limit_price": 111.0, "quantity": 7, "state": "confirmed"},
            {"order_id": "b5", "side": "buy", "limit_price": 110.0, "quantity": 7, "state": "confirmed"},
            {"order_id": "b6", "side": "buy", "limit_price": 109.0, "quantity": 7, "state": "confirmed"},
            {"order_id": "b7", "side": "buy", "limit_price": 108.0, "quantity": 7, "state": "confirmed"},
            {"order_id": "b8", "side": "buy", "limit_price": 107.0, "quantity": 7, "state": "confirmed"},
        ]
        # Total notional of open orders: 7*(114+113+112+111+110+109+108+107) = 7*884 = 6188
        # When these orders are in flight, cash_available is reduced by their notional
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 115.00,
            "atr": 4.00,
            "cash_available": 8000.0 - 6188.0,  # Reduced by open buy notional
            "shares_available": 0,
            "open_orders": perfect_orders,
        }
        result = plan_orders(runtime_state, grid_state)

        # Should have no cancels and no places (fixpoint)
        self.assertEqual(len(result["cancels"]), 0, f"Should have no cancels, got {result['cancels']}")
        self.assertEqual(len(result["places"]), 0, f"Should have no places, got {result['places']}")


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
        """When a buy fills and spot drops below, CHANGE E: place sell from open_lots."""
        # Initial: grid established with buys
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "initialized": True,
        }
        # Spot dropped slightly; CHANGE E requires open_lots to determine exit prices
        # If the buy at 115 filled, there's a lot at cost_basis=115
        # Exit price = round(115 + 1.0, 2) = 116.00
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 114.00,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 8,  # filled buy at 115 → 8 shares
            "open_orders": [],  # the buy at 115 is gone (filled)
            "open_lots": [  # CHANGE E: open_lots define exit prices
                {"open_lot_id": "lot1", "quantity": 8, "cost_basis": 115.00}
            ],
        }
        result = plan_orders(runtime_state, grid_state)
        # Should now place a sell at exit_price = 115 + step = 116
        sell_orders = [p for p in result["places"] if p["side"] == "sell"]
        self.assertGreater(len(sell_orders), 0, "Should place sell orders for swing capture")
        # Sell should be at the exit price
        self.assertIn(116.00, [s["limit_price"] for s in sell_orders])


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
    """CHANGE F: Reanchoring never happens (lattice makes it unnecessary)."""

    def test_reanchor_never_happens_above(self):
        """Spot far above band: CHANGE F lattice keeps anchor fixed."""
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Upper band: 115 + 8*1 = 123, spot = 130 → would reanchor in v3, but not in v4
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 130.00,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, grid_state)
        self.assertFalse(result["diagnostics"]["reanchored"])  # Never reanchors in v4
        self.assertAlmostEqual(result["grid_state"]["anchor"], 115.00, places=2)  # Unchanged
        # lot_dollars now computed dynamically, not preserved
        # equity_at_cost = 8000, lot = 8000/10 = 800
        self.assertAlmostEqual(result["grid_state"]["lot_dollars"], 800.0, places=0)

    def test_reanchor_never_happens_below(self):
        """Spot far below band: CHANGE F lattice keeps anchor fixed."""
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "lot_dollars": 1000.0,
            "initialized": True,
        }
        # Lower band: 115 - 8*1 = 107, spot = 100 → would reanchor in v3, but not in v4
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 100.00,
            "atr": 4.00,
            "cash_available": 8000.0,
            "shares_available": 0,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, grid_state)
        self.assertFalse(result["diagnostics"]["reanchored"])  # Never reanchors in v4
        self.assertAlmostEqual(result["grid_state"]["anchor"], 115.00, places=2)  # Unchanged
        # lot_dollars now computed dynamically
        self.assertAlmostEqual(result["grid_state"]["lot_dollars"], 800.0, places=0)


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
        # CHANGE D: lot_dollars now computed dynamically from equity_at_cost
        # equity = 8000 + 0*avg_cost = 8000, lot = 8000/10 = 800
        self.assertAlmostEqual(result2["grid_state"]["lot_dollars"], 800.0, places=1)

    def test_self_heal_zero_lot_dollars(self):
        """Grid with lot_dollars=0 but initialized=True → CHANGE D dynamic lot_dollars fixes it."""
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
        # Should NOT re-initialize (already initialized, just recompute lot_dollars)
        self.assertFalse(result["diagnostics"]["initialized_now"])
        # CHANGE D: lot_dollars is computed dynamically, not taken from grid_state
        self.assertGreater(result["grid_state"]["lot_dollars"], 0.0)
        self.assertAlmostEqual(result["grid_state"]["lot_dollars"], 800.0, places=1)


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

    def test_cloud_default_no_reanchor(self):
        """CHANGE F: v4 lattice means reanchoring never happens, even with ALLOW_REANCHOR=True."""
        # Spot far beyond band should NOT reanchor in v4 (lattice keeps anchor fixed)
        grid_state = {
            "anchor": 115.00,
            "step": 1.00,
            "initialized": True,
        }
        # Upper band: 115 + 8*1 = 123, spot = 130 (beyond band)
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

        # Should NOT reanchor (lattice means no reanchoring in v4)
        self.assertFalse(result["diagnostics"]["reanchored"])
        # Anchor should stay fixed
        self.assertAlmostEqual(result["grid_state"]["anchor"], 115.00, places=2)


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


class TestChangeCOneLotPerLevelGuard(unittest.TestCase):
    """CHANGE C: one-lot-per-level buy guard (mirrors core cases from test_cloud_reconciler.py)."""

    ANCHOR = 115.19
    STEP = 0.48
    LOT_DOLLARS = 115.62

    def _fixed_grid(self, lot_dollars=None):
        return {
            "anchor": self.ANCHOR,
            "step": self.STEP,
            "lot_dollars": lot_dollars if lot_dollars is not None else self.LOT_DOLLARS,
            "initialized": True,
        }

    def test_held_full_lot_suppresses_buy_at_that_line(self):
        """Full lot already held at 112.79 -> no buy there; lower lines still get buys."""
        # CHANGE D: lot_dollars computed dynamically. To get lot≈115.62:
        # equity = cash + shares*share_val = cash + 1*113 ≈ 1156.2 → cash ≈ 1043.2
        # Use fixed grid to ensure step=0.48
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 1043.0,
            "shares_available": 1,
            "open_orders": [],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 1, "cost_basis": 112.79}],
        }
        result = plan_orders(runtime_state, grid_state, {"ALLOW_REANCHOR": False})
        buy_prices = [p["limit_price"] for p in result["places"] if p["side"] == "buy"]
        # With dynamic lot≈115.62: qty@112.79=floor(115.62/112.79)=1, held=1 >= 1 → suppressed
        self.assertNotIn(112.79, buy_prices, "Buy at fully-held line 112.79 should be suppressed")
        # Lower line 112.31: qty=floor(115.62/112.31)=1, not held, should be placed
        self.assertIn(112.31, buy_prices, "Buy at lower line 112.31 should still be placed")
        # Sell should be placed for the exit at cost_basis+step=112.79+0.48=113.27
        sell_orders = [p for p in result["places"] if p["side"] == "sell"]
        self.assertGreater(len(sell_orders), 0, "Sell placement should be unaffected by buy guard")
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 1)

    def test_kept_open_buy_order_cancelled_when_full_lot_held(self):
        """An existing open buy order at a fully-held line must be cancelled, not kept."""
        # CHANGE D: equity ≈ 1156.2 → lot ≈ 115.62
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 1156.2,
            "shares_available": 0,
            "open_orders": [
                {"order_id": "buy_112_79", "side": "buy", "limit_price": 112.79, "quantity": 1, "state": "confirmed"},
            ],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 1, "cost_basis": 112.79}],
        }
        result = plan_orders(runtime_state, grid_state, {"ALLOW_REANCHOR": False})
        self.assertIn("buy_112_79", result["cancels"])
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 1)

    def test_price_improved_lot_suppresses_nearest_line(self):
        """Buy price improvement (cost_basis 112.75) still attributes to nearest line 112.79."""
        # CHANGE D: equity ≈ 1156.2 → lot ≈ 115.62
        # Use fixed grid to ensure step=0.48
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 1043.0,
            "shares_available": 1,
            "open_orders": [],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 1, "cost_basis": 112.75}],
        }
        result = plan_orders(runtime_state, grid_state, {"ALLOW_REANCHOR": False})
        buy_prices = [p["limit_price"] for p in result["places"] if p["side"] == "buy"]
        # Cost_basis 112.75 → i=round((112.75-115.19)/0.48)=-5 → line=115.19-5*0.48=112.79
        # Within step/2+1e-9? |112.75-112.79|=0.04 < 0.24000001 ✓
        # lot≈115.62, qty@112.79=floor(115.62/112.79)=1, held=1 >= 1 → SUPPRESSED
        self.assertNotIn(112.79, buy_prices, "Price-improved lot should still suppress nearest line 112.79")
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 1)

    def test_lot_far_outside_grid_suppresses_nothing(self):
        """A lot with cost basis far outside the grid band attributes to no line."""
        # CHANGE D: large cash, no shares with held lot
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 1156.2,
            "shares_available": 0,
            "open_orders": [],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 100, "cost_basis": 90.00}],
        }
        result = plan_orders(runtime_state, grid_state, {"ALLOW_REANCHOR": False})
        buy_prices = [p["limit_price"] for p in result["places"] if p["side"] == "buy"]
        # Cost 90 is far below anchor 115.19, doesn't attribute to any line
        self.assertIn(112.79, buy_prices, "Out-of-grid lot must not suppress any line")
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 0)

    def test_partial_holding_does_not_suppress(self):
        """Held qty below the level's full desired qty must NOT suppress the buy."""
        # CHANGE D: To get lot_dollars≈240: equity≈2400, cash+shares*share_val≈2400
        # With current_price=113, shares=1: cash + 1*113 ≈ 2400 → cash ≈ 2287
        # But we need to use fixed grid with step=0.48, so pass initialized grid_state
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 2287.0,
            "shares_available": 1,
            "open_orders": [],
            "open_lots": [{"open_lot_id": "lot1", "quantity": 1, "cost_basis": 112.79}],
        }
        # lot_dollars computed dynamically: equity=2400, lot=240
        # desired qty at 112.79 = floor(240/112.79) = 2; held=1 < 2 → NOT suppressed
        result = plan_orders(runtime_state, grid_state, {"ALLOW_REANCHOR": False})
        buys = {p["limit_price"]: p["quantity"] for p in result["places"] if p["side"] == "buy"}
        self.assertIn(112.79, buys, "Partial holding must not suppress the buy")
        self.assertEqual(buys[112.79], 2)
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 0)

    def test_no_open_lots_regression(self):
        """Absent open_lots -> behavior identical to before; no suppression diagnostics."""
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
        runtime_state = {
            "symbol": "HOOD",
            "current_price": 113.00,
            "atr": 4.20,
            "cash_available": 1156.2,
            "shares_available": 1,
            "open_orders": [],
        }
        result = plan_orders(runtime_state, grid_state, {"ALLOW_REANCHOR": False})
        buy_prices = [p["limit_price"] for p in result["places"] if p["side"] == "buy"]
        self.assertIn(112.79, buy_prices)
        # When open_lots is absent/empty, buys_suppressed_level_guard should be 0
        self.assertEqual(result["diagnostics"]["buys_suppressed_level_guard"], 0)

    def test_multi_cycle_pileup_prevention(self):
        """
        ACCEPTANCE TEST: price oscillates around the 112.79 buy line for 6 cycles.
        Each time the 112.79 buy is placed it fills instantly (share held, open_lot
        recorded); its paired sell never fills. With CHANGE C guard, held shares at
        112.79 must never exceed the level's desired qty (1) — i.e. no pileup.
        """
        # CHANGE D: Start with equity≈1156.2 → lot≈115.62
        # Use fixed grid to ensure step=0.48
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
        cash_available = 1156.2
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
            result = plan_orders(runtime_state, grid_state, {"ALLOW_REANCHOR": False})

            cancel_ids = set(result["cancels"])
            open_orders = [o for o in open_orders if o["order_id"] not in cancel_ids]

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
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
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
        result = plan_orders(runtime_state, grid_state)
        self.assertEqual(result["cancels"], [])
        self.assertEqual(len(result["places"]), 0)
        self.assertEqual(result["diagnostics"]["lot_dollars"], 92.23)
        self.assertAlmostEqual(result["diagnostics"]["unsettled_cash"], 338.86, places=1)
        for place in result["places"]:
            self.assertNotIn("tax_lots", place, "No place should carry tax_lots in live state")

    def test_2_lot_collapse_without_cash_total(self):
        """Lot collapses when cash_total absent but exits still kept."""
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
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
        result = plan_orders(runtime_state, grid_state)
        self.assertEqual(result["cancels"], [])
        self.assertEqual(len(result["places"]), 0)
        # lot = (21.86 + 5*112.31) / 10 = 583.41 / 10 = 58.34
        self.assertAlmostEqual(result["diagnostics"]["lot_dollars"], 58.34, places=2)

    def test_3_deposit_settled_buys_resume(self):
        """Deposit settlement resumes buys with correct lot_dollars."""
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
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
        result = plan_orders(runtime_state, grid_state)
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

    def test_4_selectable_mixed_tax_lots(self):
        """Exit placement respects selectable flag (tax_lots is cloud_reconciler feature)."""
        grid_state = {"anchor": 100.00, "step": 0.48, "initialized": True}
        runtime_state = {
            "symbol": "HOOD", "current_price": 99.00, "atr": 1.0,
            "cash_available": 8000.0, "shares_available": 2, "cash_total": 8000.0,
            "open_orders": [],
            "open_lots": [
                {"open_lot_id": "lot_sel", "quantity": 1, "cost_basis": 100.00, "is_selectable": True},
                {"open_lot_id": "lot_no_sel", "quantity": 1, "cost_basis": 100.48, "is_selectable": False},
            ],
        }
        result = plan_orders(runtime_state, grid_state)
        # Exits at 100.48 (from 100.00) and 100.96 (from 100.48)
        sell_places = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_places), 2, "Should place exits from both lots")
        # Verify both exits are generated regardless of is_selectable
        prices = sorted([p["limit_price"] for p in sell_places])
        self.assertAlmostEqual(prices[0], 100.48, places=2)
        self.assertAlmostEqual(prices[1], 100.96, places=2)

    def test_5_sells_frozen_no_lots(self):
        """Shares held but no lots => sells are frozen, untouched."""
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
        runtime_state = {
            "symbol": "HOOD", "current_price": 110.00, "atr": 1.0,
            "cash_available": 8000.0, "shares_available": 1, "cash_total": 8000.0,
            "open_orders": [
                {"order_id": "s1", "side": "sell", "limit_price": 111.00, "quantity": 1},
                {"order_id": "s2", "side": "sell", "limit_price": 111.50, "quantity": 1},
            ],
            "open_lots": [],  # NO lots, but shares are held
        }
        result = plan_orders(runtime_state, grid_state)
        # Sells should NOT be cancelled
        self.assertNotIn("s1", result["cancels"], "Open sell s1 should not be cancelled")
        self.assertNotIn("s2", result["cancels"], "Open sell s2 should not be cancelled")
        # No new sell places should be generated
        sell_places = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_places), 0, "No new sell places when frozen")
        self.assertTrue(result["diagnostics"]["sells_frozen_no_lots"])

    def test_6_exit_deferred_below_spot(self):
        """Exits with price <= spot are deferred."""
        grid_state = {"anchor": 100.00, "step": 0.48, "initialized": True}

        # Part A: spot=105, exit @100.48 is below spot → deferred
        runtime_state = {
            "symbol": "HOOD", "current_price": 105.00, "atr": 1.0,
            "cash_available": 8000.0, "shares_available": 1,
            "open_orders": [],
            "open_lots": [
                {"open_lot_id": "lot1", "quantity": 1, "cost_basis": 100.00, "is_selectable": True},
            ],
        }
        result = plan_orders(runtime_state, grid_state)
        sell_places = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_places), 0, "No sell at 100.48 when spot=105")
        self.assertGreaterEqual(result["diagnostics"]["exits_deferred_below_spot"], 1)

        # Part B: spot=99, exit @100.48 is above spot → placed
        runtime_state["current_price"] = 99.00
        result = plan_orders(runtime_state, grid_state)
        sell_places = [p for p in result["places"] if p["side"] == "sell"]
        self.assertEqual(len(sell_places), 1, "Should have sell @100.48 when spot=99")
        self.assertAlmostEqual(sell_places[0]["limit_price"], 100.48, places=2)

    def test_7_buffer_accounting(self):
        """Buffer accounting: buffer_dollars = buffer_lots * lot_dollars."""
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
        runtime_state = {
            "symbol": "HOOD", "current_price": 107.40, "atr": 1.0,
            "cash_available": 1150.40, "cash_total": 1150.40, "shares_available": 0,
            "open_orders": [],
            "open_lots": [],
        }
        result = plan_orders(runtime_state, grid_state)
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
        grid_state = {"anchor": 115.19, "step": 0.48, "initialized": True}
        runtime_state = {
            "symbol": "HOOD", "current_price": 100.00, "atr": 1.0,
            "cash_available": 2000.0, "cash_total": 2000.0, "shares_available": 0,
            "open_orders": [], "open_lots": [],
        }
        result = plan_orders(runtime_state, grid_state)
        buy_places = [p for p in result["places"] if p["side"] == "buy"]
        self.assertEqual(len(buy_places), 8)

    def test_fixpoint_with_lattice(self):
        """Test fixpoint: perfect book yields no cancels/places."""
        runtime_state = {
            "symbol": "HOOD", "current_price": 115.19, "atr": 1.0,
            "cash_available": 500.0, "cash_total": 500.0, "shares_available": 0,
            "open_orders": [], "open_lots": [],
        }
        result1 = plan_orders(runtime_state, {})
        grid_state = result1["grid_state"]

        perfect_orders = [
            {"order_id": f"order_{i}", "side": p["side"], "limit_price": p["limit_price"], "quantity": p["quantity"]}
            for i, p in enumerate(result1["places"])
        ]

        runtime_state["open_orders"] = perfect_orders
        result2 = plan_orders(runtime_state, grid_state)
        self.assertEqual(len(result2["cancels"]), 0)
        self.assertEqual(len(result2["places"]), 0)


if __name__ == "__main__":
    unittest.main()
