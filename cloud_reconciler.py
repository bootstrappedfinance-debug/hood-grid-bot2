#!/usr/bin/env python3
"""
Compact cloud reconciler for HOOD grid bot.

Self-contained, stdlib only. Fixed geometry, no persistence, no reanchoring.
Produces plans identical to grid_engine.py in ALLOW_REANCHOR=False mode.

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
LOT_DOLLARS = 115.62
NUM_LEVELS = 8
TICK = 0.01
TIME_IN_FORCE = "gtc"
MARKET_HOURS = "regular_hours"


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


def reconcile(runtime_state, anchor, step, lot_dollars, num_levels=NUM_LEVELS, tick=TICK):
    """
    Core reconciliation logic (stateless, fixed geometry).

    Input: {symbol, current_price, cash_available, shares_available, open_orders}
    Output: {cancels, places, diagnostics}
    """
    current_price = round(runtime_state["current_price"], 2)
    cash_available = runtime_state["cash_available"]
    shares_available = runtime_state["shares_available"]
    open_orders = runtime_state.get("open_orders", [])

    # Generate grid
    lines = grid_lines(anchor, step, num_levels)

    # Split into buy and sell lines (strictly below/above spot, nearest-first)
    buy_lines = sorted([l for l in lines if l < current_price], reverse=True)[:num_levels]
    sell_lines = sorted([l for l in lines if l > current_price])[:num_levels]

    # Reconcile open orders
    kept_orders = {}
    cancelled_orders = []
    lines_with_kept = set()

    for order in open_orders:
        order_id = order["order_id"]
        side = order["side"]
        order_price = round(order["limit_price"], 2)
        order_qty = order["quantity"]

        # Try to match to a grid line
        matched_line = None
        if side == "buy":
            for line in buy_lines:
                if abs(order_price - line) <= tick / 2:
                    matched_line = line
                    break
        else:  # sell
            for line in sell_lines:
                if abs(order_price - line) <= tick / 2:
                    matched_line = line
                    break

        # Keep if price matches, qty matches, line not already taken
        if matched_line is not None:
            qty = desired_qty(lot_dollars, matched_line)
            if qty >= 1 and order_qty == qty and matched_line not in lines_with_kept:
                kept_orders[order_id] = order
                lines_with_kept.add(matched_line)
                continue

        # Cancel order
        cancelled_orders.append(order)

    # Calculate available budget (cancelled orders release resources)
    cancelled_buy_cash = sum(
        o["limit_price"] * o["quantity"] for o in cancelled_orders if o["side"] == "buy"
    )
    cancelled_sell_shares = sum(o["quantity"] for o in cancelled_orders if o["side"] == "sell")

    available_for_buys = cash_available + cancelled_buy_cash
    available_for_sells = shares_available + cancelled_sell_shares

    # Build places (empty lines, respecting budget)
    places = []

    # Buy side (nearest-first)
    remaining_budget = available_for_buys
    for line in buy_lines:
        if line in lines_with_kept:
            continue
        qty = desired_qty(lot_dollars, line)
        if qty < 1:
            continue
        cost = qty * line
        if cost <= remaining_budget:
            places.append(
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

    # Sell side (nearest-first)
    remaining_shares = available_for_sells
    for line in sell_lines:
        if line in lines_with_kept:
            continue
        qty = desired_qty(lot_dollars, line)
        if qty < 1:
            continue
        if qty <= remaining_shares:
            places.append(
                {
                    "side": "sell",
                    "limit_price": round(line, 2),
                    "quantity": qty,
                    "time_in_force": TIME_IN_FORCE,
                    "market_hours": MARKET_HOURS,
                }
            )
            remaining_shares -= qty
        else:
            break

    # Diagnostics
    num_buys = len([p for p in places if p["side"] == "buy"])
    num_sells = len([p for p in places if p["side"] == "sell"])
    cash_budget_used = sum(p["limit_price"] * p["quantity"] for p in places if p["side"] == "buy")
    shares_budget_used = sum(p["quantity"] for p in places if p["side"] == "sell")

    return {
        "cancels": [o["order_id"] for o in cancelled_orders],
        "places": places,
        "diagnostics": {
            "anchor": round(anchor, 2),
            "step": round(step, 2),
            "lot_dollars": round(lot_dollars, 2),
            "spot": current_price,
            "num_buys": num_buys,
            "num_sells": num_sells,
            "cash_budget_used": round(cash_budget_used, 2),
            "shares_budget_used": shares_budget_used,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Cloud reconciler (fixed geometry, no persistence)")
    parser.add_argument("--state-file", required=True, help="Runtime state JSON")
    parser.add_argument("--out", help="Output file (default: stdout)")
    parser.add_argument("--anchor", type=float, default=ANCHOR, help="Grid anchor")
    parser.add_argument("--step", type=float, default=STEP, help="Grid step")
    parser.add_argument("--lot", type=float, default=LOT_DOLLARS, help="Lot dollars")

    args = parser.parse_args()

    try:
        with open(args.state_file) as f:
            runtime_state = json.load(f)

        result = reconcile(runtime_state, args.anchor, args.step, args.lot)

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
