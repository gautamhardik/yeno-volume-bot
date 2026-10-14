#!/usr/bin/env python3
"""YeNo Apex Bot - Elite Volume Bot Challenge Competitor.

Built strictly against the official YeNo × Builderr API and Evaluator Contract:
- Target: $1,000 eligible volume in 24 hours.
- Starts with $10 simulated cash.
- Maximum $5 cash per BUY (fee-inclusive).
- Terminal flat finish at the qualifying crossing point.
- Ranks #1 by Settled Cash at qualifying crossing.
"""

from __future__ import annotations

import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Strategic Configuration
MIN_REFERENCE_GAP_USD = 36.0
MAX_SPREAD_CENTS = 0.03
MAX_IMMEDIATE_EXIT_LOSS_USD = 0.38
TARGET_PROFIT_PER_SHARE_USD = 0.045
MIN_PROFIT_FLOOR_USD = 0.20
SCRATCH_SECONDS_TO_CLOSE = 30.0
FORCED_EXIT_SECONDS_TO_CLOSE = 18.0
ENTRY_MIN_SECONDS_TO_CLOSE = 22.0
ENTRY_MAX_SECONDS_TO_CLOSE = 110.0
MAX_CYCLES_PER_MARKET = 4
REENTRY_COOLDOWN_SECONDS = 0.8
MAX_REFERENCE_AGE_SECONDS = 1.5
STOP_LOSS_PER_SHARE_USD = 0.10


def _levels(rows: Any, *, reverse: bool) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            price, size = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and size > 0:
            output.append((price, size))
    return sorted(output, reverse=reverse)


def _fees(fills: list[tuple[float, float]]) -> float:
    gross = sum(price * shares for price, shares in fills)
    protocol = sum(0.07 * shares * price * (1.0 - price) for price, shares in fills)
    return round(protocol + 1e-12, 5) + round(gross * 0.01 + 1e-12, 5)


def _buy_cost_for_shares(asks: Any, shares: float) -> tuple[float, float] | None:
    remaining = shares
    fills: list[tuple[float, float]] = []
    for price, available in _levels(asks, reverse=False):
        quantity = min(remaining, available)
        fills.append((price, quantity))
        remaining -= quantity
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    return sum(price * quantity for price, quantity in fills), _fees(fills)


def _sell_proceeds_for_shares(bids: Any, shares: float) -> tuple[float, float] | None:
    remaining = shares
    fills: list[tuple[float, float]] = []
    for price, available in _levels(bids, reverse=True):
        quantity = min(remaining, available)
        fills.append((price, quantity))
        remaining -= quantity
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    return sum(price * quantity for price, quantity in fills), _fees(fills)


def _calculate_buy_order(
    asks: Any, available_cash: float, min_order_size: float
) -> tuple[float, float, float] | None:
    """Calculate target shares and fee-inclusive cash ceiling."""
    levels = _levels(asks, reverse=False)
    if not levels:
        return None

    best_p = levels[0][0]
    # Sizing ceiling: up to $4.85, safely inside $5.00 rules cap
    spend_cash = min(4.85, available_cash - 0.04)
    k = best_p * (1.01 + 0.07 * (1.0 - best_p))
    affordable_shares = spend_cash / k

    if affordable_shares < min_order_size - 1e-9:
        return None

    # Target: 5.2 shares if funds allow, else exact min_size (e.g. 5.0)
    target_shares = min(affordable_shares, 5.2 if spend_cash >= 4.50 else min_order_size)

    cost_info = _buy_cost_for_shares(levels, target_shares)
    if cost_info is None:
        return None

    gross, fees = cost_info
    total_cost = gross + fees
    if total_cost > spend_cash + 1e-9:
        return None

    return target_shares, gross, fees


class ApexBot:
    def __init__(self) -> None:
        self.market_id: str | None = None
        self.position_was_open = False
        self.completed_cycles = 0
        self.mkt_completed_cycles = 0
        self.last_flat_at = -math.inf
        self.last_buy_attempt_at = -math.inf
        self.loss_in_market = False
        self.strike_crossings = 0
        self.last_sign = 0
        self.qualified = False

    def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._decide(observation)
        except (KeyError, TypeError, ValueError, OverflowError):
            return {"action": "HOLD"}

    def _decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        market = observation["market"]
        account = observation["account"]
        books = observation["books"]
        rules = observation.get("rules", {})
        now = float(observation["timestamp"])
        seconds = float(market["secondsToClose"])
        market_id = str(market["id"])
        position = account.get("position")

        eligible_volume = float(account.get("eligibleVolumeUsd", 0))

        # Handle market transition
        if market_id != self.market_id:
            self.market_id = market_id
            self.position_was_open = False
            self.mkt_completed_cycles = 0
            self.last_flat_at = -math.inf
            self.last_buy_attempt_at = -math.inf
            self.loss_in_market = False
            self.strike_crossings = 0
            self.last_sign = 0

        # Track strike crossings for chop detection
        reference = observation.get("reference")
        current_gap = 0.0
        if isinstance(reference, dict) and "btcMidUsd" in reference:
            current_gap = float(reference["btcMidUsd"]) - float(reference["openingTargetUsd"])
            sign = 1 if current_gap > 8.0 else (-1 if current_gap < -8.0 else 0)
            if sign != 0 and self.last_sign != 0 and sign != self.last_sign:
                self.strike_crossings += 1
            if sign != 0:
                self.last_sign = sign

        if position is None and self.position_was_open:
            self.completed_cycles += 1
            self.mkt_completed_cycles += 1
            self.last_flat_at = now
        self.position_was_open = position is not None

        # -------------------------------------------------------------
        # 1. QUALIFICATION FREEZE
        # Crossing $1,000 while flat qualifies. Freeze immediately!
        # -------------------------------------------------------------
        if eligible_volume >= 1000.0 and position is None:
            self.qualified = True
            return {"action": "HOLD"}

        # -------------------------------------------------------------
        # 2. ACTIVE POSITION MANAGEMENT (EXIT ENGINE)
        # -------------------------------------------------------------
        if position is not None:
            side = str(position["outcome"])
            shares = float(position["shares"])
            paid = float(position["buy_gross_usd"]) + float(position["buy_fees_usd"])
            bids = books.get(side, {}).get("bids", [])

            executable = _sell_proceeds_for_shares(bids, shares)
            if executable is None:
                if seconds <= FORCED_EXIT_SECONDS_TO_CLOSE:
                    return {"action": "SELL"}
                return {"action": "HOLD"}

            sell_gross, sell_fees = executable
            received = (
                float(position.get("sell_gross_usd", 0))
                - float(position.get("sell_fees_usd", 0))
                + sell_gross
                - sell_fees
            )
            net_pnl = received - paid

            # 1. Target Profit Exit: let winner run to 0.95+
            min_profit = max(TARGET_PROFIT_PER_SHARE_USD * shares, MIN_PROFIT_FLOOR_USD)
            if net_pnl >= min_profit:
                return {"action": "SELL"}

            # 2. Scratch near expiry
            if seconds <= SCRATCH_SECONDS_TO_CLOSE and net_pnl >= -0.005 * shares:
                return {"action": "SELL"}

            # 3. Deadline exit before 15s freeze
            if seconds <= FORCED_EXIT_SECONDS_TO_CLOSE:
                if net_pnl < 0:
                    self.loss_in_market = True
                return {"action": "SELL"}

            # 4. Underlying thesis flipped against us
            if side == "YES" and current_gap < 0.0:
                self.loss_in_market = True
                return {"action": "SELL"}
            if side == "NO" and current_gap > 0.0:
                self.loss_in_market = True
                return {"action": "SELL"}

            # 5. Stop Loss if severe adverse move
            if net_pnl <= -STOP_LOSS_PER_SHARE_USD * shares:
                self.loss_in_market = True
                return {"action": "SELL"}

            return {"action": "HOLD"}

        # -------------------------------------------------------------
        # 3. ENTRY EVALUATION & DYNAMIC SIZING
        # -------------------------------------------------------------
        if self.mkt_completed_cycles >= MAX_CYCLES_PER_MARKET:
            return {"action": "HOLD"}

        # If a loss occurred in this market, abort this market
        if self.loss_in_market:
            return {"action": "HOLD"}

        # Reject choppy oscillating markets
        if self.strike_crossings >= 2:
            return {"action": "HOLD"}

        if now - self.last_flat_at < REENTRY_COOLDOWN_SECONDS - 1e-9:
            return {"action": "HOLD"}

        if now - self.last_buy_attempt_at < REENTRY_COOLDOWN_SECONDS - 1e-9:
            return {"action": "HOLD"}

        if not (ENTRY_MIN_SECONDS_TO_CLOSE <= seconds <= ENTRY_MAX_SECONDS_TO_CLOSE):
            return {"action": "HOLD"}

        if not isinstance(reference, dict) or bool(reference.get("targetProvisional", True)):
            return {"action": "HOLD"}

        if now - float(reference.get("observedAt", -math.inf)) > MAX_REFERENCE_AGE_SECONDS:
            return {"action": "HOLD"}

        if float(reference.get("targetObservedAt", math.inf)) > now + 1e-9:
            return {"action": "HOLD"}

        if abs(current_gap) < MIN_REFERENCE_GAP_USD - 1e-9:
            return {"action": "HOLD"}

        side = "YES" if current_gap > 0 else "NO"
        opp_side = "NO" if side == "YES" else "YES"

        asks = books.get(side, {}).get("asks", [])
        bids = books.get(side, {}).get("bids", [])
        opp_asks = books.get(opp_side, {}).get("asks", [])

        best_asks = _levels(asks, reverse=False)
        best_bids = _levels(bids, reverse=True)
        opp_best_asks = _levels(opp_asks, reverse=False)

        if not best_asks or not best_bids:
            return {"action": "HOLD"}

        best_ask_price = best_asks[0][0]
        best_bid_price = best_bids[0][0]

        # Spread filter
        if best_ask_price - best_bid_price > MAX_SPREAD_CENTS + 1e-9:
            return {"action": "HOLD"}

        # Order book consensus check
        if opp_best_asks and best_ask_price < opp_best_asks[0][0]:
            return {"action": "HOLD"}

        # Dynamic price window based on cash level
        cash_usd = float(account["cashUsd"])
        min_p = 0.65 if cash_usd < 4.40 else 0.72
        if not (min_p <= best_ask_price <= 0.93):
            return {"action": "HOLD"}

        min_order_size = float(books.get(side, {}).get("minOrderSize", 5))

        calc_result = _calculate_buy_order(asks, cash_usd, min_order_size)
        if calc_result is None:
            return {"action": "HOLD"}

        target_shares, gross_cost, fees_cost = calc_result
        total_buy_cash = gross_cost + fees_cost

        # Liquidity pre-check: ensure bids can absorb these shares
        immediate_exit = _sell_proceeds_for_shares(bids, target_shares)
        if immediate_exit is None:
            return {"action": "HOLD"}

        sell_gross, sell_fees = immediate_exit
        immediate_loss = (gross_cost + fees_cost) - (sell_gross - sell_fees)
        if immediate_loss > MAX_IMMEDIATE_EXIT_LOSS_USD + 1e-9:
            return {"action": "HOLD"}

        self.last_buy_attempt_at = now
        return {
            "action": "BUY",
            "outcome": side,
            "maxCashUsd": round(total_buy_cash, 4),
        }


BOT = ApexBot()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/decide":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            response = BOT.decide(payload)
            body = json.dumps(response, separators=(",", ":")).encode()
        except Exception:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
