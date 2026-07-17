#!/usr/bin/env python3
"""
Compact cloud reconciler for HOOD grid bot.

Self-contained, stdlib only. Fixed geometry, no persistence, no reanchoring.
Produces plans identical to grid_engine.py in ALLOW_REANCHOR=False mode.

Enhancements:
- CHANGE A: Dynamic lot sizing from equity (cost-basis + cash)
- CHANGE G (v5): Sells are standard limit orders — tax-lot designation removed (designated orders could be rejected for unselectable lots, leaving no standing sell)

Use: python3 cloud_reconciler.py --state-file runtime.json [--out plan.json] [--anchor A --step S --lot L]
"""

import json
import math
import sys
import argparse
from pathlib import Path

# Fixed grid geometry (overridable via CLI flags)
ANCHOR = 115.19
STEP = 0.48
LOT_DOLLARS = 115.62  # Default lot; overridden by --lot or computed dynamically
NUM_LEVELS = 8
TICK = 0.01
TIME_IN_FORCE = "gtc"
MARKET_HOURS = "regular_hours"
BUFFER_LOTS = 2  # CHANGE D: number of lots to hold in buffer


def floor(x):
    """Integer floor."""
    return int(math.floor(x))


def desired_qty(lot_dollars, line_price):
    """Quantity at a grid line. Returns 0 if < 1."""
    if lot_dollars <= 0 or line_price <= 0:
        return 0
    qty = floor(lot_dollars / line_price)
    return qty if qty >= 1 else 0


def grid_lines(anchor, step, num_levels):
    """Generate 17 symmetric grid lines."""
    return [round(anchor + i * step, 2) for i in range(-num_levels, num_levels + 1)]


def held_shares_by_line(open_lots, anchor, step, num_levels):
    """
    CHANGE C: Attribute held share lots to their nearest grid line.
    CHANGE F: Remove band restriction (band-unrestricted lattice).

    Nearest-line attribution handles buy price improvement (fill cost slightly
    below the limit price): a lot attributes to line L when its cost basis is
    within step/2 of L. Lots can now attribute to ANY lattice line, not just
    those within the original fixed band.

    Returns {line_price: total_held_qty}.
    """
    held = {}
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


def compute_dynamic_lot(runtime_state, num_levels=NUM_LEVELS, buffer_lots=BUFFER_LOTS):
    """
    CHANGE A: Compute lot_dollars from equity at cost.
    CHANGE D: Add settlement-aware equity + cash buffer.

    Returns (lot_dollars, equity_at_cost, total_shares, avg_cost_used, unsettled_cash).
    """
    current_price = round(runtime_state["current_price"], 2)
    cash_available = runtime_state["cash_available"]
    cash_total = runtime_state.get("cash_total")  # optional
    shares_available = runtime_state["shares_available"]
    open_orders = runtime_state.get("open_orders", [])
    average_cost = runtime_state.get("average_cost", 0)

    # CHANGE D: Calculate open_buy_notional and unsettled cash
    open_buy_notional = sum(
        o["limit_price"] * o["quantity"] for o in open_orders if o["side"] == "buy"
    )
    if cash_total is not None:
        unsettled = max(0, cash_total - cash_available - open_buy_notional)
    else:
        unsettled = 0.0

    # Total cash includes open buys and unsettled proceeds
    total_cash = cash_available + open_buy_notional + unsettled

    # Total shares includes open sell orders (not yet filled)
    total_shares = shares_available + sum(
        o["quantity"] for o in open_orders if o["side"] == "sell"
    )

    # Share valuation: cost basis if available, else current price
    share_val = average_cost if average_cost > 0 else current_price

    # Equity at cost (what we have if we closed today at current price)
    equity_at_cost = total_cash + total_shares * share_val

    # CHANGE D: Lot size with buffer
    lot_dollars = round(equity_at_cost / (num_levels + buffer_lots), 2)

    return lot_dollars, equity_at_cost, total_shares, share_val, unsettled


def reconcile(runtime_state, anchor, step, lot_dollars_override=None, num_levels=NUM_LEVELS, buffer_lots=BUFFER_LOTS, tick=TICK):
    """
    Core reconciliation logic (v5 with CHANGE D/E/F/G).

    Input: {symbol, current_price, cash_available, shares_available, open_orders, average_cost?, open_lots?, cash_total?}
    Output: {cancels, places, diagnostics}

    Args:
        lot_dollars_override: if provided, use as fixed lot (skip dynamic computation)
        Otherwise, compute lot_dollars dynamically from equity_at_cost with settlement-aware buffer
    """
    current_price = round(runtime_state["current_price"], 2)
    cash_available = runtime_state["cash_available"]
    shares_available = runtime_state["shares_available"]
    open_orders = runtime_state.get("open_orders", [])
    open_lots = runtime_state.get("open_lots", [])

    # CHANGE D: Compute or use fixed lot_dollars (with buffer_lots)
    if lot_dollars_override is not None:
        lot_dollars = lot_dollars_override
        equity_at_cost = None
        total_shares = shares_available
        avg_cost_used = None
        unsettled = 0.0
    else:
        lot_dollars, equity_at_cost, total_shares, avg_cost_used, unsettled = compute_dynamic_lot(
            runtime_state, num_levels, buffer_lots
        )

    # CHANGE F: Generate buy_lines using sliding lattice (not fixed band)
    if step <= 0:
        return {
            "cancels": [],
            "places": [],
            "diagnostics": {
                "anchor": round(anchor, 2),
                "step": 0.0,
                "lot_dollars": lot_dollars,
                "spot": current_price,
                "num_buys": 0,
                "num_sells": 0,
                "cash_budget_used": 0.0,
                "shares_budget_used": 0,
                "unsettled_cash": round(unsettled, 2),
                "buffer_lots": buffer_lots,
                "buffer_dollars": 0.0,
                "lot_below_min_line": False,
                "exits_deferred_below_spot": 0,
                "sells_frozen_no_lots": False,
                "buys_suppressed_level_guard": 0,
            },
        }

    # CHANGE F: Sliding lattice buys (infinite, not fixed band)
    i_max = int(floor((current_price - anchor) / step))
    if round(anchor + i_max * step, 2) >= current_price:
        i_max -= 1
    buy_lines = [round(anchor + i * step, 2) for i in range(i_max, i_max - num_levels, -1)]

    # CHANGE E: FAIL-SAFE check: sells_frozen_no_lots
    # If shares are held but no lots data, don't touch sells (keep them as-is)
    sells_frozen = False
    open_sell_qty = sum(o["quantity"] for o in open_orders if o["side"] == "sell")
    if (shares_available + open_sell_qty > 0) and not open_lots:
        # CHANGE E: FAIL-SAFE - sells_frozen_no_lots
        sells_frozen = True

    # CHANGE E: Compute desired exits from open_lots
    desired_exits = {}  # {exit_price: qty}
    if open_lots:
        for lot in open_lots:
            exit_price = round(lot["cost_basis"] + step, 2)
            desired_exits[exit_price] = desired_exits.get(exit_price, 0) + lot["quantity"]

    # CHANGE C: one-lot-per-level buy guard (attribute held shares to grid lines)
    held_at_line = held_shares_by_line(open_lots, anchor, step, num_levels)
    suppressed_lines = set()

    # Reconcile open orders
    kept_orders = {}
    cancelled_orders = []
    lines_with_kept_buys = set()
    prices_with_kept_sells = set()

    for order in open_orders:
        order_id = order["order_id"]
        side = order["side"]
        order_price = round(order["limit_price"], 2)
        order_qty = order["quantity"]

        if side == "buy":
            # Try to match to buy_lines
            matched_line = None
            for line in buy_lines:
                if abs(order_price - line) <= tick / 2:
                    matched_line = line
                    break

            if matched_line is not None:
                qty = desired_qty(lot_dollars, matched_line)
                # CHANGE C: buy suppressed if the full lot for this line is already held
                if qty >= 1 and held_at_line.get(matched_line, 0) >= qty:
                    suppressed_lines.add(matched_line)
                elif qty >= 1 and order_qty == qty and matched_line not in lines_with_kept_buys:
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

    # Calculate available budget (cancelled orders release resources)
    cancelled_buy_cash = sum(
        o["limit_price"] * o["quantity"] for o in cancelled_orders if o["side"] == "buy"
    )
    cancelled_sell_shares = sum(o["quantity"] for o in cancelled_orders if o["side"] == "sell")

    available_for_buys = cash_available + cancelled_buy_cash
    available_for_sells = shares_available + cancelled_sell_shares

    # Build buy places
    buy_places = []
    remaining_budget = available_for_buys
    for line in buy_lines:
        if line in lines_with_kept_buys:
            continue
        qty = desired_qty(lot_dollars, line)
        if qty < 1:
            continue
        # CHANGE C: skip (not break) placing a buy where the full lot is already held
        if held_at_line.get(line, 0) >= qty:
            suppressed_lines.add(line)
            continue
        cost = qty * line
        if cost <= remaining_budget:
            buy_places.append(
                {
                    "side": "buy",
                    "limit_price": round(line, 2),
                    "quantity": qty,
                    "time_in_force": TIME_IN_FORCE,
                    "market_hours": MARKET_HOURS,
                }
            )
            remaining_budget -= cost
        else:
            break

    # CHANGE E: Build sell places from desired exits
    sell_places = []
    exits_deferred_below_spot = 0

    if not sells_frozen:
        # Place desired exits (in ascending price order, nearest first)
        remaining_shares = available_for_sells
        for exit_price in sorted(desired_exits.keys()):
            if exit_price in prices_with_kept_sells:
                continue  # already have order at this price

            # CHANGE E: Defer exits <= spot (would be market orders)
            if exit_price <= current_price:
                exits_deferred_below_spot += desired_exits[exit_price]
                continue

            desired_qty_at_price = desired_exits[exit_price]
            if desired_qty_at_price <= remaining_shares:
                sell_places.append({
                    "side": "sell",
                    "limit_price": exit_price,
                    "quantity": desired_qty_at_price,
                    "time_in_force": TIME_IN_FORCE,
                    "market_hours": MARKET_HOURS,
                })
                remaining_shares -= desired_qty_at_price
            else:
                break

    places = buy_places + sell_places

    # Diagnostics
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

    diag = {
        "anchor": round(anchor, 2),
        "step": round(step, 2),
        "lot_dollars": round(lot_dollars, 2),
        "spot": current_price,
        "num_buys": num_buys,
        "num_sells": num_sells,
        "cash_budget_used": round(cash_budget_used, 2),
        "shares_budget_used": shares_budget_used,
        "unsettled_cash": round(unsettled, 2),
        "buffer_lots": buffer_lots,
        "buffer_dollars": buffer_dollars,
        "lot_below_min_line": lot_below_min_line,
        "exits_deferred_below_spot": exits_deferred_below_spot,
        "sells_frozen_no_lots": sells_frozen,
    }

    # Add dynamic lot diagnostics if computed
    if equity_at_cost is not None:
        diag["equity_at_cost"] = round(equity_at_cost, 2)
        diag["total_shares"] = int(total_shares) if total_shares == int(total_shares) else total_shares
        diag["avg_cost_used"] = round(avg_cost_used, 2) if avg_cost_used else None

    # Add CHANGE C level-guard suppression count
    if open_lots:
        diag["buys_suppressed_level_guard"] = len(suppressed_lines)

    return {
        "cancels": [o["order_id"] for o in cancelled_orders],
        "places": places,
        "diagnostics": diag,
    }


def main():
    parser = argparse.ArgumentParser(description="Cloud reconciler (fixed geometry, no persistence)")
    parser.add_argument("--state-file", required=True, help="Runtime state JSON")
    parser.add_argument("--out", help="Output file (default: stdout)")
    parser.add_argument("--anchor", type=float, default=ANCHOR, help="Grid anchor")
    parser.add_argument("--step", type=float, default=STEP, help="Grid step")
    parser.add_argument("--lot", type=float, default=None, help="Fixed lot (overrides dynamic computation)")
    parser.add_argument("--buffer-lots", type=int, default=BUFFER_LOTS, help="CHANGE D: Number of buffer lots (default 2)")

    args = parser.parse_args()

    try:
        with open(args.state_file) as f:
            runtime_state = json.load(f)

        result = reconcile(runtime_state, args.anchor, args.step, lot_dollars_override=args.lot, buffer_lots=args.buffer_lots)

        output = {"cancels": result["cancels"], "places": result["places"], "diagnostics": result["diagnostics"]}

        print(json.dumps(output, indent=2))
        if args.out:
            with open(args.out, "w") as f:
                json.dump(output, f, indent=2)

        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
