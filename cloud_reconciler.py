#!/usr/bin/env python3
"""
Compact cloud reconciler for HOOD grid bot.

Self-contained, stdlib only. Fixed geometry, no persistence, no reanchoring.
Produces plans identical to grid_engine.py in ALLOW_REANCHOR=False mode.

Enhancements:
- CHANGE A: Dynamic lot sizing from equity (cost-basis + cash)
- CHANGE B: Specified-lot selling (tax_lots) for tax-aware order fulfillment

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

    Nearest-line attribution handles buy price improvement (fill cost slightly
    below the limit price): a lot attributes to line L when its cost basis is
    within step/2 of L. Lots whose cost basis falls outside the grid band (or
    isn't close enough to any line) attribute to no line and never suppress.

    Returns {line_price: total_held_qty}.
    """
    held = {}
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


def compute_dynamic_lot(runtime_state, num_levels=NUM_LEVELS):
    """
    CHANGE A: Compute lot_dollars from equity at cost.

    Returns (lot_dollars, equity_at_cost, total_shares, avg_cost_used).
    """
    current_price = round(runtime_state["current_price"], 2)
    cash_available = runtime_state["cash_available"]
    shares_available = runtime_state["shares_available"]
    open_orders = runtime_state.get("open_orders", [])
    average_cost = runtime_state.get("average_cost", 0)

    # Total cash includes open buy orders (not yet filled)
    total_cash = cash_available + sum(
        o["limit_price"] * o["quantity"] for o in open_orders if o["side"] == "buy"
    )

    # Total shares includes open sell orders (not yet filled)
    total_shares = shares_available + sum(
        o["quantity"] for o in open_orders if o["side"] == "sell"
    )

    # Share valuation: cost basis if available, else current price
    share_val = average_cost if average_cost > 0 else current_price

    # Equity at cost (what we have if we closed today at current price)
    equity_at_cost = total_cash + total_shares * share_val

    # Lot size
    lot_dollars = round(equity_at_cost / num_levels, 2)

    return lot_dollars, equity_at_cost, total_shares, share_val


def assign_tax_lots(sell_places, open_lots):
    """
    CHANGE B: Assign tax lots to sell orders for tax-aware fulfillment.

    Modifies sell_places in-place to add tax_lots field (if possible).
    Returns sell_fifo_fallback count and consumed_from_lots dict.

    Tax lot priority:
    1. GAIN lots (cost_basis < sell_price) first
    2. Within GAIN: highest cost_basis first (nearest below, smallest gain realized)
    3. LOSS lots (cost_basis >= sell_price) second
    4. Within LOSS: lowest cost_basis first (nearest above, smallest loss realized)
    """
    sell_fifo_fallback = 0
    consumed_from_lots = {}  # {open_lot_id: consumed_qty}

    # Sort sell_places by price (ascending) for deterministic assignment
    sell_places_sorted = sorted(
        [(i, p) for i, p in enumerate(sell_places) if p["side"] == "sell"],
        key=lambda x: x[1]["limit_price"],
    )

    for place_idx, sell_place in sell_places_sorted:
        sell_price = sell_place["limit_price"]
        sell_qty = sell_place["quantity"]

        # Find candidate lots with remaining quantity
        candidates = []
        for lot in open_lots:
            lot_id = lot["open_lot_id"]
            total_qty = lot["quantity"]
            consumed = consumed_from_lots.get(lot_id, 0)
            remaining = total_qty - consumed
            if remaining > 0:
                cost_basis = lot["cost_basis"]
                # Sort key: (is_loss, sort_cost_basis, lot_id)
                # is_loss: 0 for gain (cost < S), 1 for loss (cost >= S)
                # Within GAIN: descending cost (-cost_basis)
                # Within LOSS: ascending cost (cost_basis)
                is_loss = 0 if cost_basis < sell_price else 1
                sort_cost = -cost_basis if is_loss == 0 else cost_basis
                candidates.append((is_loss, sort_cost, lot_id, cost_basis, remaining))

        if not candidates:
            # No lots available; use FIFO fallback
            sell_fifo_fallback += 1
            continue

        # Sort candidates by (is_loss, sort_cost, lot_id)
        candidates.sort()

        # Greedily assign lots, tracking tentative consumption
        tax_lots = []
        still_needed = sell_qty
        tentative_consumed = {}  # Temporary tracking for this sell

        for is_loss, sort_cost, lot_id, cost_basis, remaining in candidates:
            if still_needed <= 0:
                break
            take_qty = min(remaining, still_needed)
            tax_lots.append({"open_lot_id": lot_id, "quantity": take_qty})
            tentative_consumed[lot_id] = take_qty
            still_needed -= take_qty

            # Cap at 30 lots per order
            if len(tax_lots) >= 30:
                break

        # Attach tax_lots only if fully covered
        if still_needed <= 0:
            # Fully covered: commit tentative consumed to global consumed_from_lots
            for lot_id, qty in tentative_consumed.items():
                consumed_from_lots[lot_id] = consumed_from_lots.get(lot_id, 0) + qty
            sell_place["tax_lots"] = tax_lots
        else:
            # Cannot fully cover (hit 30-lot cap or insufficient lots); use FIFO fallback
            # DO NOT commit tentative_consumed (roll back bookkeeping)
            sell_fifo_fallback += 1
            # Don't attach partial tax_lots

    return sell_fifo_fallback, consumed_from_lots


def reconcile(runtime_state, anchor, step, lot_dollars_override=None, num_levels=NUM_LEVELS, tick=TICK):
    """
    Core reconciliation logic (stateless, fixed geometry).

    Input: {symbol, current_price, cash_available, shares_available, open_orders, average_cost?, open_lots?}
    Output: {cancels, places, diagnostics}

    Args:
        lot_dollars_override: if provided, use as fixed lot (skip dynamic computation)
        Otherwise, compute lot_dollars dynamically from equity_at_cost
    """
    current_price = round(runtime_state["current_price"], 2)
    cash_available = runtime_state["cash_available"]
    shares_available = runtime_state["shares_available"]
    open_orders = runtime_state.get("open_orders", [])
    open_lots = runtime_state.get("open_lots", [])

    # CHANGE A: Compute or use fixed lot_dollars
    if lot_dollars_override is not None:
        lot_dollars = lot_dollars_override
        equity_at_cost = None
        total_shares = shares_available
        avg_cost_used = None
    else:
        lot_dollars, equity_at_cost, total_shares, avg_cost_used = compute_dynamic_lot(
            runtime_state, num_levels
        )

    # Generate grid
    lines = grid_lines(anchor, step, num_levels)

    # Split into buy and sell lines (strictly below/above spot, nearest-first)
    buy_lines = sorted([l for l in lines if l < current_price], reverse=True)[:num_levels]
    sell_lines = sorted([l for l in lines if l > current_price])[:num_levels]

    # CHANGE C: one-lot-per-level buy guard (attribute held shares to grid lines)
    held_at_line = held_shares_by_line(open_lots, anchor, step, num_levels)
    suppressed_lines = set()

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

        # Keep if price matches, qty matches (using computed lot_dollars), line not already taken
        if matched_line is not None:
            qty = desired_qty(lot_dollars, matched_line)
            # CHANGE C: buy suppressed if the full lot for this line is already held
            if side == "buy" and qty >= 1 and held_at_line.get(matched_line, 0) >= qty:
                suppressed_lines.add(matched_line)
            elif qty >= 1 and order_qty == qty and matched_line not in lines_with_kept:
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
        # CHANGE C: skip (not break) placing a buy where the full lot is already held
        if held_at_line.get(line, 0) >= qty:
            suppressed_lines.add(line)
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

    # CHANGE B: Assign tax lots to sell orders
    sell_fifo_fallback = 0
    if open_lots:
        sell_fifo_fallback, _ = assign_tax_lots(places, open_lots)

    # Diagnostics
    num_buys = len([p for p in places if p["side"] == "buy"])
    num_sells = len([p for p in places if p["side"] == "sell"])
    cash_budget_used = sum(p["limit_price"] * p["quantity"] for p in places if p["side"] == "buy")
    shares_budget_used = sum(p["quantity"] for p in places if p["side"] == "sell")

    diag = {
        "anchor": round(anchor, 2),
        "step": round(step, 2),
        "lot_dollars": round(lot_dollars, 2),
        "spot": current_price,
        "num_buys": num_buys,
        "num_sells": num_sells,
        "cash_budget_used": round(cash_budget_used, 2),
        "shares_budget_used": shares_budget_used,
    }

    # Add dynamic lot diagnostics if computed
    if equity_at_cost is not None:
        diag["equity_at_cost"] = round(equity_at_cost, 2)
        diag["total_shares"] = int(total_shares) if total_shares == int(total_shares) else total_shares
        diag["avg_cost_used"] = round(avg_cost_used, 2) if avg_cost_used else None

    # Add tax lot fallback count and CHANGE C level-guard suppression count
    if open_lots:
        diag["sell_fifo_fallback"] = sell_fifo_fallback
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

    args = parser.parse_args()

    try:
        with open(args.state_file) as f:
            runtime_state = json.load(f)

        result = reconcile(runtime_state, args.anchor, args.step, lot_dollars_override=args.lot)

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
