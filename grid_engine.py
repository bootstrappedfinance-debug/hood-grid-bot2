#!/usr/bin/env python3
"""
Deterministic grid-trading engine for HOOD stock.

Pure Python 3.9+, stdlib only. Emits buy/sell order plans based on account state
and a persisted grid configuration. Core design: fixpoint idempotence (stable grids
are never thrashed). All prices rounded to 2 decimals, quantities via floor().

Module-level config (defaults; can be overridden via config parameter):
  SYMBOL = "HOOD"
  NUM_LEVELS = 8          # grid lines each side of spot
  ATR_STEP_FRACTION = 0.25
  MIN_STEP = 0.25
  TICK = 0.01
  TIME_IN_FORCE = "gtc"
  MARKET_HOURS = "regular_hours"
"""

import sys
import json
import math
import argparse
from pathlib import Path
from typing import Optional, Dict, Any, List


# Module-level config (can be loaded from config.json, but these are defaults)
SYMBOL = "HOOD"
NUM_LEVELS = 8
ATR_STEP_FRACTION = 0.25
MIN_STEP = 0.25
TICK = 0.01
TIME_IN_FORCE = "gtc"
MARKET_HOURS = "regular_hours"
ALLOW_REANCHOR = True  # Cloud stateless deployment: set to False for fixed geometry mode


def _round_to_tick(price: float, tick: float = TICK) -> float:
    """Round price to nearest tick."""
    return round(price / tick) * tick


def _desired_qty(lot_dollars: float, line_price: float) -> int:
    """
    Calculate desired quantity for a grid line.

    Returns floor(lot_dollars / line_price), or 0 if result is < 1.
    This unified helper ensures consistent sizing across order matching and placement.
    """
    if lot_dollars <= 0 or line_price <= 0:
        return 0
    qty = int(math.floor(lot_dollars / line_price))
    return qty if qty >= 1 else 0


def initialize_grid(
    current_price: float,
    atr: float,
    cash_available: float,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Initialize grid state.

    Returns a dict with:
      anchor: float - price around which grid is centered
      step: float - distance between grid lines
      lot_dollars: float - notional size per grid level
      initialized: bool - always True
      created_at: str - ISO timestamp (optional, added by caller if needed)

    Args:
      current_price: current market price
      atr: 14-period ATR in dollars
      cash_available: total available cash
      config: optional config overrides
    """
    cfg = _get_config(config)
    num_levels = cfg.get("NUM_LEVELS", NUM_LEVELS)
    atr_step_frac = cfg.get("ATR_STEP_FRACTION", ATR_STEP_FRACTION)
    min_step = cfg.get("MIN_STEP", MIN_STEP)
    tick = cfg.get("TICK", TICK)

    # step = max(atr * fraction, min_step), then round to tick
    step = max(atr * atr_step_frac, min_step)
    step = _round_to_tick(step, tick)
    step = round(step, 2)  # ensure 2 decimals

    anchor = round(current_price, 2)
    lot_dollars = round(cash_available / num_levels, 2)

    return {
        "anchor": anchor,
        "step": step,
        "lot_dollars": lot_dollars,
        "initialized": True,
    }


def needs_reanchor(
    grid_state: Dict[str, Any], current_price: float, config: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Check if current price has drifted beyond the grid band.

    Returns True if price > anchor + NUM_LEVELS*step or price < anchor - NUM_LEVELS*step.
    """
    if not grid_state or not grid_state.get("initialized"):
        return False

    cfg = _get_config(config)
    num_levels = cfg.get("NUM_LEVELS", NUM_LEVELS)

    anchor = grid_state.get("anchor", 0.0)
    step = grid_state.get("step", 0.0)

    upper_bound = anchor + num_levels * step
    lower_bound = anchor - num_levels * step

    return current_price > upper_bound or current_price < lower_bound


def grid_lines(
    grid_state: Dict[str, Any], config: Optional[Dict[str, Any]] = None
) -> List[float]:
    """
    Generate all grid lines.

    Returns 17-element list from anchor-NUM_LEVELS*step to anchor+NUM_LEVELS*step,
    symmetric around anchor, all rounded to 2 decimals.
    """
    if not grid_state or not grid_state.get("initialized"):
        return []

    cfg = _get_config(config)
    num_levels = cfg.get("NUM_LEVELS", NUM_LEVELS)

    anchor = grid_state.get("anchor", 0.0)
    step = grid_state.get("step", 0.0)

    lines = []
    for i in range(-num_levels, num_levels + 1):
        line = anchor + i * step
        lines.append(round(line, 2))

    return lines


def held_shares_by_line(
    open_lots: List[Dict[str, Any]], anchor: float, step: float, num_levels: int
) -> Dict[float, int]:
    """
    CHANGE C: Attribute held share lots to their nearest grid line.

    Nearest-line attribution handles buy price improvement (fill cost slightly
    below the limit price): a lot attributes to line L when its cost basis is
    within step/2 of L. Lots whose cost basis falls outside the grid band (or
    isn't close enough to any line) attribute to no line and never suppress.

    Returns {line_price: total_held_qty}.
    """
    held: Dict[float, int] = {}
    if step <= 0:
        return held
    for lot in open_lots:
        cost_basis = lot["cost_basis"]
        qty = lot["quantity"]
        i = round((cost_basis - anchor) / step)
        if -num_levels <= i <= num_levels:
            line = round(anchor + i * step, 2)
            if abs(cost_basis - line) <= step / 2 + 1e-9:
                held[line] = held.get(line, 0) + qty
    return held


def plan_orders(
    runtime_state: Dict[str, Any],
    grid_state: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Core planning algorithm.

    Analyzes current account state and grid, returns a plan of order cancels and places.

    Input runtime_state:
      symbol, current_price, atr, cash_available, shares_available, open_orders

    Input grid_state:
      anchor, step, lot_dollars, initialized (or empty {} on first run)

    Returns dict:
      cancels: [order_id, ...]
      places: [{side, limit_price, quantity, time_in_force, market_hours}, ...]
      grid_state: updated grid state dict (anchored/initialized)
      diagnostics: {step, lot_dollars, anchor, spot, num_buys, num_sells,
                    cash_budget_used, shares_budget_used, reanchored, initialized_now,
                    skipped_no_cash}

    Algorithm ensures:
      - No buy placed at price >= current_price
      - No sell placed at price <= current_price
      - Total buy notional <= available_for_buys
      - Total sell qty <= available_for_sells
      - FIXPOINT: if open_orders exactly match desired grid, returns empty cancels/places
    """
    cfg = _get_config(config)
    num_levels = cfg.get("NUM_LEVELS", NUM_LEVELS)
    time_in_force = cfg.get("TIME_IN_FORCE", TIME_IN_FORCE)
    market_hours = cfg.get("MARKET_HOURS", MARKET_HOURS)
    allow_reanchor = cfg.get("ALLOW_REANCHOR", ALLOW_REANCHOR)

    current_price = round(runtime_state["current_price"], 2)
    atr = runtime_state["atr"]
    cash_available = runtime_state["cash_available"]
    shares_available = runtime_state["shares_available"]
    open_orders = runtime_state.get("open_orders", [])
    open_lots = runtime_state.get("open_lots", [])  # CHANGE C: held tax lots (optional)

    # Copy grid_state so we can mutate it
    gs = dict(grid_state) if grid_state else {}
    initialized_now = False
    reanchored = False
    skipped_no_cash = False
    not_initialized = False

    # Step 1: Initialize or reanchor grid
    # Cloud stateless mode: ALLOW_REANCHOR=False means fixed grid geometry (no init, no reanchor)
    if not allow_reanchor:
        # Stateless cloud mode: use provided grid exactly as-is, never init or reanchor
        if not gs or not gs.get("initialized"):
            # No valid grid provided; cannot proceed
            not_initialized = True
            return {
                "cancels": [],
                "places": [],
                "grid_state": gs,  # return as-is (empty/uninitialized)
                "diagnostics": {
                    "step": 0.0,
                    "lot_dollars": 0.0,
                    "anchor": 0.0,
                    "spot": current_price,
                    "num_buys": 0,
                    "num_sells": 0,
                    "cash_budget_used": 0.0,
                    "shares_budget_used": 0,
                    "reanchored": False,
                    "initialized_now": False,
                    "skipped_no_cash": False,
                    "not_initialized": True,
                    "buys_suppressed_level_guard": 0,  # CHANGE C
                },
            }
        # Valid grid provided; use it exactly as-is (no reanchor)
    else:
        # Standard mode: allow initialization and reanchoring
        # BUG FIX 2: Do NOT initialize if cash <= 0 (prevents lot_dollars from locking at 0)
        if not gs or not gs.get("initialized"):
            if cash_available <= 0:
                # Skip initialization; return empty plan
                skipped_no_cash = True
                return {
                    "cancels": [],
                    "places": [],
                    "grid_state": gs,  # remain uninitialized
                    "diagnostics": {
                        "step": 0.0,
                        "lot_dollars": 0.0,
                        "anchor": 0.0,
                        "spot": current_price,
                        "num_buys": 0,
                        "num_sells": 0,
                        "cash_budget_used": 0.0,
                        "shares_budget_used": 0,
                        "reanchored": False,
                        "initialized_now": False,
                        "skipped_no_cash": True,
                        "not_initialized": False,
                        "buys_suppressed_level_guard": 0,  # CHANGE C
                    },
                }
            gs = initialize_grid(current_price, atr, cash_available, config)
            initialized_now = True
        # BUG FIX 2 (defense): If grid exists but lot_dollars <= 0, re-init if cash > 0
        elif gs.get("lot_dollars", 0.0) <= 0 and cash_available > 0:
            gs = initialize_grid(current_price, atr, cash_available, config)
            initialized_now = True
        elif needs_reanchor(gs, current_price, config):
            reanchored = True
            gs["anchor"] = round(current_price, 2)
            gs["step"] = _round_to_tick(max(atr * cfg.get("ATR_STEP_FRACTION", ATR_STEP_FRACTION), cfg.get("MIN_STEP", MIN_STEP)), cfg.get("TICK", TICK))
            gs["step"] = round(gs["step"], 2)
            # Preserve lot_dollars across reanchor

    # Step 2: Generate grid lines
    lines = grid_lines(gs, config)
    # BUG FIX 1: buy_lines must be NEAREST 8, not FARTHEST 8
    # Sort descending so highest (nearest to spot) comes first, then take first 8
    buy_lines = sorted([line for line in lines if line < current_price], reverse=True)[:num_levels]

    # sell_lines already ascending (lowest/nearest first is correct for lines > spot)
    sell_lines = sorted([line for line in lines if line > current_price])[:num_levels]

    # Step 3: Match open orders to grid and identify keepers vs cancellations
    kept_orders = {}
    cancelled_orders = []
    lines_with_kept_orders = set()

    lot_dollars = gs.get("lot_dollars", 0.0)
    tick = cfg.get("TICK", TICK)

    # CHANGE C: one-lot-per-level buy guard (attribute held shares to grid lines)
    held_at_line = held_shares_by_line(open_lots, gs.get("anchor", 0.0), gs.get("step", 0.0), num_levels)
    suppressed_lines = set()

    for order in open_orders:
        order_id = order["order_id"]
        side = order["side"]
        order_price = round(order["limit_price"], 2)
        order_qty = order["quantity"]

        # Try to match this order to a grid line
        matched_line = None
        is_buy_side = side == "buy"

        if is_buy_side:
            for line in buy_lines:
                if abs(order_price - line) <= tick / 2:
                    matched_line = line
                    break
        else:  # sell
            for line in sell_lines:
                if abs(order_price - line) <= tick / 2:
                    matched_line = line
                    break

        # Check if this order should be kept
        if matched_line is not None:
            # BUG FIX 3: Use unified _desired_qty helper
            desired_qty = _desired_qty(lot_dollars, matched_line)
            # CHANGE C: buy suppressed if the full lot for this line is already held
            if is_buy_side and desired_qty >= 1 and held_at_line.get(matched_line, 0) >= desired_qty:
                suppressed_lines.add(matched_line)
            # Only keep if qty matches desired qty AND line not already occupied
            # (Lines with desired_qty < 1 must cancel; they're not placed)
            elif desired_qty >= 1 and order_qty == desired_qty and matched_line not in lines_with_kept_orders:
                kept_orders[order_id] = order
                lines_with_kept_orders.add(matched_line)
                continue

        # Order doesn't match desired grid → cancel
        cancelled_orders.append(order)

    # Step 4: Calculate available budget (cancelled orders release resources first)
    cancelled_buy_cash = sum(
        o["limit_price"] * o["quantity"] for o in cancelled_orders if o["side"] == "buy"
    )
    cancelled_sell_shares = sum(o["quantity"] for o in cancelled_orders if o["side"] == "sell")

    available_for_buys = cash_available + cancelled_buy_cash
    available_for_sells = shares_available + cancelled_sell_shares

    # Step 5: Build places for empty lines, respecting budget
    places = []

    # Buy side: iterate buy_lines nearest-first (already descending/highest-first)
    remaining_buy_budget = available_for_buys
    for line in buy_lines:
        if line in lines_with_kept_orders:
            continue  # already have order here

        # BUG FIX 3: Use unified _desired_qty helper
        desired_qty = _desired_qty(lot_dollars, line)
        if desired_qty < 1:
            continue  # too small to place

        # CHANGE C: skip (not break) placing a buy where the full lot is already held
        if held_at_line.get(line, 0) >= desired_qty:
            suppressed_lines.add(line)
            continue

        cost = desired_qty * line
        if cost <= remaining_buy_budget:
            places.append(
                {
                    "side": "buy",
                    "limit_price": round(line, 2),
                    "quantity": desired_qty,
                    "time_in_force": time_in_force,
                    "market_hours": market_hours,
                }
            )
            remaining_buy_budget -= cost
        else:
            break  # budget exhausted

    # Sell side: iterate sell_lines nearest-first (already ascending/lowest-first)
    remaining_sell_shares = available_for_sells
    for line in sell_lines:
        if line in lines_with_kept_orders:
            continue  # already have order here

        # BUG FIX 3: Use unified _desired_qty helper
        desired_qty = _desired_qty(lot_dollars, line)
        if desired_qty < 1:
            continue  # too small to place

        if desired_qty <= remaining_sell_shares:
            places.append(
                {
                    "side": "sell",
                    "limit_price": round(line, 2),
                    "quantity": desired_qty,
                    "time_in_force": time_in_force,
                    "market_hours": market_hours,
                }
            )
            remaining_sell_shares -= desired_qty
        else:
            break  # shares exhausted

    # Step 6: Build diagnostics
    num_buys = len([p for p in places if p["side"] == "buy"])
    num_sells = len([p for p in places if p["side"] == "sell"])
    cash_budget_used = sum(
        p["limit_price"] * p["quantity"] for p in places if p["side"] == "buy"
    )
    shares_budget_used = sum(p["quantity"] for p in places if p["side"] == "sell")

    # Step 7: Return full plan
    cancels = [o["order_id"] for o in cancelled_orders]

    return {
        "cancels": cancels,
        "places": places,
        "grid_state": gs,
        "diagnostics": {
            "step": round(gs.get("step", 0.0), 2),
            "lot_dollars": round(gs.get("lot_dollars", 0.0), 2),
            "anchor": round(gs.get("anchor", 0.0), 2),
            "spot": current_price,
            "num_buys": num_buys,
            "num_sells": num_sells,
            "cash_budget_used": round(cash_budget_used, 2),
            "shares_budget_used": shares_budget_used,
            "reanchored": reanchored,
            "initialized_now": initialized_now,
            "skipped_no_cash": skipped_no_cash,
            "not_initialized": not_initialized,
            "buys_suppressed_level_guard": len(suppressed_lines),  # CHANGE C
        },
    }


def _get_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Get effective config (overrides or defaults)."""
    defaults = {
        "SYMBOL": SYMBOL,
        "NUM_LEVELS": NUM_LEVELS,
        "ATR_STEP_FRACTION": ATR_STEP_FRACTION,
        "MIN_STEP": MIN_STEP,
        "TICK": TICK,
        "TIME_IN_FORCE": TIME_IN_FORCE,
        "MARKET_HOURS": MARKET_HOURS,
        "ALLOW_REANCHOR": ALLOW_REANCHOR,
    }
    if config:
        defaults.update(config)
    return defaults


# CLI
def cmd_plan(args):
    """Execute 'plan' command."""
    try:
        # Read runtime state
        with open(args.state_file, "r") as f:
            runtime_state = json.load(f)

        # Read grid state (missing file → {})
        grid_state = {}
        if Path(args.grid_file).exists():
            with open(args.grid_file, "r") as f:
                content = f.read().strip()
                if content:
                    grid_state = json.loads(content)

        # Run plan_orders
        result = plan_orders(runtime_state, grid_state)

        # Write updated grid state (unless dry-run)
        if not args.dry_run:
            with open(args.grid_file, "w") as f:
                json.dump(result["grid_state"], f, indent=2)

        # Print plan JSON to stdout
        plan_output = {
            "cancels": result["cancels"],
            "places": result["places"],
            "diagnostics": result["diagnostics"],
        }
        print(json.dumps(plan_output, indent=2))

        # Write to --out file if provided
        if args.out:
            with open(args.out, "w") as f:
                json.dump(plan_output, f, indent=2)

        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def cmd_reset(args):
    """Execute 'reset' command."""
    try:
        grid_file = Path(args.grid_file)
        if grid_file.exists():
            grid_file.unlink()
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description="Grid trading engine CLI")
    subparsers = parser.add_subparsers(dest="command", help="command to run")

    # 'plan' subcommand
    plan_parser = subparsers.add_parser("plan", help="Run planning cycle")
    plan_parser.add_argument(
        "--state-file",
        required=True,
        help="Path to runtime JSON state",
    )
    plan_parser.add_argument(
        "--grid-file",
        required=True,
        help="Path to persisted grid state JSON",
    )
    plan_parser.add_argument(
        "--out",
        help="Optional output file for plan JSON",
    )
    plan_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write grid state back to file",
    )
    plan_parser.set_defaults(func=cmd_plan)

    # 'reset' subcommand
    reset_parser = subparsers.add_parser("reset", help="Reset grid state")
    reset_parser.add_argument(
        "--grid-file",
        required=True,
        help="Path to grid state file to delete",
    )
    reset_parser.set_defaults(func=cmd_reset)

    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
