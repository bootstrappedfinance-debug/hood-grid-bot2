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
BUFFER_LOTS = 2  # CHANGE D: number of lots to hold in buffer (settled cash buffer)


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
    # CHANGE D: divide by (num_levels + buffer_lots) to account for buffer
    buffer_lots = cfg.get("BUFFER_LOTS", BUFFER_LOTS)
    lot_dollars = round(cash_available / (num_levels + buffer_lots), 2)

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
    CHANGE F: Vestigial function (lattice makes reanchoring unnecessary).

    Always returns False. Anchor is now a fixed lattice origin and never changes.
    Buys follow price via sliding lattice (nearest lines below spot).
    """
    # CHANGE F: Lattice makes reanchoring unnecessary - anchor is fixed
    return False


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
    CHANGE F: Remove band restriction (band-unrestricted lattice).

    Nearest-line attribution handles buy price improvement (fill cost slightly
    below the limit price): a lot attributes to line L when its cost basis is
    within step/2 of L. Lots can now attribute to ANY lattice line, not just
    those within the original fixed band.

    Returns {line_price: total_held_qty}.
    """
    held: Dict[float, int] = {}
    if step <= 0:
        return held
    for lot in open_lots:
        cost_basis = lot["cost_basis"]
        qty = lot["quantity"]
        i = round((cost_basis - anchor) / step)
        # CHANGE F: removed -num_levels <= i <= num_levels restriction (infinite lattice)
        line = round(anchor + i * step, 2)
        if abs(cost_basis - line) <= step / 2 + 1e-9:
            held[line] = held.get(line, 0) + qty
    return held


def compute_dynamic_lot(
    runtime_state: Dict[str, Any],
    num_levels: int,
    buffer_lots: int,
) -> tuple:
    """
    CHANGE D: Compute lot_dollars from equity at cost, settlement-aware.

    Returns (lot_dollars, unsettled_cash).
    """
    current_price = round(runtime_state["current_price"], 2)
    cash_available = runtime_state["cash_available"]
    cash_total = runtime_state.get("cash_total")  # optional
    shares_available = runtime_state["shares_available"]
    open_orders = runtime_state.get("open_orders", [])
    average_cost = runtime_state.get("average_cost", 0)

    # Calculate unsettled cash
    open_buy_notional = sum(
        o["limit_price"] * o["quantity"] for o in open_orders if o["side"] == "buy"
    )
    if cash_total is not None:
        unsettled = max(0, cash_total - cash_available - open_buy_notional)
    else:
        unsettled = 0.0

    # Total cash includes open buys and unsettled proceeds
    total_cash = cash_available + open_buy_notional + unsettled

    # Total shares includes open sells
    total_shares = shares_available + sum(
        o["quantity"] for o in open_orders if o["side"] == "sell"
    )

    # Share valuation
    share_val = average_cost if average_cost > 0 else current_price

    # Equity at cost
    equity_at_cost = total_cash + total_shares * share_val

    # Lot size with buffer
    lot_dollars = round(equity_at_cost / (num_levels + buffer_lots), 2)

    return lot_dollars, unsettled


def plan_orders(
    runtime_state: Dict[str, Any],
    grid_state: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Core planning algorithm (v4 with CHANGE D/E/F).

    Analyzes current account state and grid, returns a plan of order cancels and places.

    Input runtime_state:
      symbol, current_price, atr, cash_available, shares_available, open_orders, open_lots, cash_total (optional)

    Input grid_state:
      anchor, step, initialized (or empty {} on first run)

    Returns dict:
      cancels: [order_id, ...]
      places: [{side, limit_price, quantity, time_in_force, market_hours}, ...]
      grid_state: updated grid state dict (initialized)
      diagnostics: {step, lot_dollars, anchor, spot, num_buys, num_sells,
                    cash_budget_used, shares_budget_used, initialized_now, skipped_no_cash,
                    unsettled_cash, buffer_lots, buffer_dollars, lot_below_min_line,
                    exits_deferred_below_spot, sells_frozen_no_lots, reanchored (always False),
                    not_initialized, buys_suppressed_level_guard}
    """
    cfg = _get_config(config)
    num_levels = cfg.get("NUM_LEVELS", NUM_LEVELS)
    buffer_lots = cfg.get("BUFFER_LOTS", BUFFER_LOTS)
    time_in_force = cfg.get("TIME_IN_FORCE", TIME_IN_FORCE)
    market_hours = cfg.get("MARKET_HOURS", MARKET_HOURS)
    allow_reanchor = cfg.get("ALLOW_REANCHOR", ALLOW_REANCHOR)
    tick = cfg.get("TICK", TICK)

    current_price = round(runtime_state["current_price"], 2)
    atr = runtime_state["atr"]
    cash_available = runtime_state["cash_available"]
    shares_available = runtime_state["shares_available"]
    open_orders = runtime_state.get("open_orders", [])
    open_lots = runtime_state.get("open_lots", [])  # CHANGE E: for exits

    # Copy grid_state so we can mutate it
    gs = dict(grid_state) if grid_state else {}
    initialized_now = False
    skipped_no_cash = False
    not_initialized = False

    # Step 1: Initialize grid (CHANGE D: divide by num_levels + buffer_lots)
    if not allow_reanchor:
        # Stateless cloud mode
        if not gs or not gs.get("initialized"):
            not_initialized = True
            return {
                "cancels": [],
                "places": [],
                "grid_state": gs,
                "diagnostics": {
                    "step": 0.0,
                    "lot_dollars": 0.0,
                    "anchor": 0.0,
                    "spot": current_price,
                    "num_buys": 0,
                    "num_sells": 0,
                    "cash_budget_used": 0.0,
                    "shares_budget_used": 0,
                    "unsettled_cash": 0.0,
                    "buffer_lots": buffer_lots,
                    "buffer_dollars": 0.0,
                    "lot_below_min_line": False,
                    "exits_deferred_below_spot": 0,
                    "sells_frozen_no_lots": False,
                    "reanchored": False,
                    "initialized_now": False,
                    "skipped_no_cash": False,
                    "not_initialized": True,
                    "buys_suppressed_level_guard": 0,
                },
            }
    else:
        # Standard mode: allow initialization (never reanchors with lattice CHANGE F)
        if not gs or not gs.get("initialized"):
            if cash_available <= 0:
                skipped_no_cash = True
                return {
                    "cancels": [],
                    "places": [],
                    "grid_state": gs,
                    "diagnostics": {
                        "step": 0.0,
                        "lot_dollars": 0.0,
                        "anchor": 0.0,
                        "spot": current_price,
                        "num_buys": 0,
                        "num_sells": 0,
                        "cash_budget_used": 0.0,
                        "shares_budget_used": 0,
                        "unsettled_cash": 0.0,
                        "buffer_lots": buffer_lots,
                        "buffer_dollars": 0.0,
                        "lot_below_min_line": False,
                        "exits_deferred_below_spot": 0,
                        "sells_frozen_no_lots": False,
                        "reanchored": False,
                        "initialized_now": False,
                        "skipped_no_cash": True,
                        "not_initialized": False,
                        "buys_suppressed_level_guard": 0,
                    },
                }
            gs = initialize_grid(current_price, atr, cash_available, config)
            initialized_now = True
        # CHANGE F: never reanchor (lattice makes it meaningless)

    # Step 2: CHANGE D - Compute lot_dollars dynamically
    lot_dollars, unsettled_cash = compute_dynamic_lot(runtime_state, num_levels, buffer_lots)
    gs["lot_dollars"] = lot_dollars  # Store for diagnostics

    # Step 3: CHANGE F - Generate buy_lines using sliding lattice
    anchor = gs.get("anchor", 0.0)
    step = gs.get("step", 0.0)
    if step <= 0:
        return {
            "cancels": [],
            "places": [],
            "grid_state": gs,
            "diagnostics": {
                "step": 0.0,
                "lot_dollars": lot_dollars,
                "anchor": anchor,
                "spot": current_price,
                "num_buys": 0,
                "num_sells": 0,
                "cash_budget_used": 0.0,
                "shares_budget_used": 0,
                "unsettled_cash": unsettled_cash,
                "buffer_lots": buffer_lots,
                "buffer_dollars": 0.0,
                "lot_below_min_line": False,
                "exits_deferred_below_spot": 0,
                "sells_frozen_no_lots": False,
                "reanchored": False,
                "initialized_now": initialized_now,
                "skipped_no_cash": skipped_no_cash,
                "not_initialized": not_initialized,
                "buys_suppressed_level_guard": 0,
            },
        }

    # CHANGE F: Compute buy_lines from infinite lattice
    if step > 0:
        i_max = int(math.floor((current_price - anchor) / step))
        if round(anchor + i_max * step, 2) >= current_price:
            i_max -= 1
        buy_lines = [round(anchor + i * step, 2) for i in range(i_max, i_max - num_levels, -1)]
    else:
        buy_lines = []

    # Step 4: CHANGE E - FAIL-SAFE check: sells_frozen_no_lots
    # If shares are held but no lots data, don't touch sells (keep them as-is)
    sells_frozen = False
    open_sell_qty = sum(o["quantity"] for o in open_orders if o["side"] == "sell")
    if (shares_available + open_sell_qty > 0) and not open_lots:
        # CHANGE E: FAIL-SAFE - sells_frozen_no_lots
        sells_frozen = True

    # Step 5: Compute desired exits from open_lots
    desired_exits = {}  # {exit_price: qty}
    if open_lots:
        for lot in open_lots:
            exit_price = round(lot["cost_basis"] + step, 2)
            desired_exits[exit_price] = desired_exits.get(exit_price, 0) + lot["quantity"]

    # Step 6: Match open orders to desired state
    kept_orders = {}
    cancelled_orders = []
    lines_with_kept_buys = set()
    prices_with_kept_sells = set()

    # CHANGE C: one-lot-per-level buy guard
    held_at_line = held_shares_by_line(open_lots, anchor, step, num_levels)
    suppressed_lines = set()

    for order in open_orders:
        order_id = order["order_id"]
        side = order["side"]
        order_price = round(order["limit_price"], 2)
        order_qty = order["quantity"]

        if side == "buy":
            # Match to buy_lines
            matched_line = None
            for line in buy_lines:
                if abs(order_price - line) <= tick / 2:
                    matched_line = line
                    break

            if matched_line is not None:
                desired_qty = _desired_qty(lot_dollars, matched_line)
                if desired_qty >= 1 and held_at_line.get(matched_line, 0) >= desired_qty:
                    suppressed_lines.add(matched_line)
                elif desired_qty >= 1 and order_qty == desired_qty and matched_line not in lines_with_kept_buys:
                    kept_orders[order_id] = order
                    lines_with_kept_buys.add(matched_line)
                    continue

            cancelled_orders.append(order)

        else:  # sell
            # CHANGE E: If sells are frozen, keep all existing sells as-is
            if sells_frozen:
                kept_orders[order_id] = order
                continue

            # CHANGE E: Match to desired exits from lots
            matched_exit_price = None
            if order_price in desired_exits and order_qty == desired_exits[order_price]:
                if order_price not in prices_with_kept_sells:
                    matched_exit_price = order_price
                    prices_with_kept_sells.add(order_price)

            if matched_exit_price is not None:
                kept_orders[order_id] = order
                continue

            cancelled_orders.append(order)

    # Step 6: Calculate available budget
    cancelled_buy_cash = sum(
        o["limit_price"] * o["quantity"] for o in cancelled_orders if o["side"] == "buy"
    )
    cancelled_sell_shares = sum(o["quantity"] for o in cancelled_orders if o["side"] == "sell")

    available_for_buys = cash_available + cancelled_buy_cash
    available_for_sells = shares_available + cancelled_sell_shares

    # Step 7: CHANGE E - Build sell places from desired exits
    sell_places = []
    exits_deferred_below_spot = 0

    if not sells_frozen:
        # Place desired exits (in ascending price order, nearest first)
        remaining_sell_shares = available_for_sells
        for exit_price in sorted(desired_exits.keys()):
            if exit_price in prices_with_kept_sells:
                continue  # already have order at this price

            # CHANGE E: Defer exits <= spot (would be market orders)
            if exit_price <= current_price:
                exits_deferred_below_spot += desired_exits[exit_price]
                continue

            desired_qty = desired_exits[exit_price]
            if desired_qty <= remaining_sell_shares:
                sell_places.append({
                    "side": "sell",
                    "limit_price": exit_price,
                    "quantity": desired_qty,
                    "time_in_force": time_in_force,
                    "market_hours": market_hours,
                })
                remaining_sell_shares -= desired_qty
            else:
                break

    # Step 8: Build buy places
    buy_places = []
    remaining_buy_budget = available_for_buys
    for line in buy_lines:
        if line in lines_with_kept_buys:
            continue

        desired_qty = _desired_qty(lot_dollars, line)
        if desired_qty < 1:
            continue

        if held_at_line.get(line, 0) >= desired_qty:
            suppressed_lines.add(line)
            continue

        cost = desired_qty * line
        if cost <= remaining_buy_budget:
            buy_places.append({
                "side": "buy",
                "limit_price": round(line, 2),
                "quantity": desired_qty,
                "time_in_force": time_in_force,
                "market_hours": market_hours,
            })
            remaining_buy_budget -= cost
        else:
            break

    places = buy_places + sell_places

    # Step 9: Build diagnostics
    num_buys = len(buy_places)
    num_sells = len(sell_places)
    cash_budget_used = sum(p["limit_price"] * p["quantity"] for p in buy_places)
    shares_budget_used = sum(p["quantity"] for p in sell_places)

    # CHANGE D: Compute lot_below_min_line
    lot_below_min_line = False
    if buy_lines and lot_dollars > 0:
        nearest_buy_line = max(buy_lines)  # highest buy line (nearest to spot)
        if lot_dollars < nearest_buy_line:
            lot_below_min_line = True

    buffer_dollars = round(buffer_lots * lot_dollars, 2)

    # Step 10: Return full plan
    cancels = [o["order_id"] for o in cancelled_orders]

    return {
        "cancels": cancels,
        "places": places,
        "grid_state": gs,
        "diagnostics": {
            "step": round(step, 2),
            "lot_dollars": round(lot_dollars, 2),
            "anchor": round(anchor, 2),
            "spot": current_price,
            "num_buys": num_buys,
            "num_sells": num_sells,
            "cash_budget_used": round(cash_budget_used, 2),
            "shares_budget_used": shares_budget_used,
            "unsettled_cash": round(unsettled_cash, 2),
            "buffer_lots": buffer_lots,
            "buffer_dollars": buffer_dollars,
            "lot_below_min_line": lot_below_min_line,
            "exits_deferred_below_spot": exits_deferred_below_spot,
            "sells_frozen_no_lots": sells_frozen,
            "reanchored": False,  # CHANGE F: never reanchors (vestigial for compat)
            "initialized_now": initialized_now,
            "skipped_no_cash": skipped_no_cash,
            "not_initialized": not_initialized,
            "buys_suppressed_level_guard": len(suppressed_lines),
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
        "BUFFER_LOTS": BUFFER_LOTS,  # CHANGE D
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
