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
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# --- Calibrated Championship Constants ---
MAX_BUY_CASH_USD = 4.88
CASH_SAFETY_BUFFER_USD = 0.03

ENTRY_MAX_SECONDS_TO_CLOSE = 135.0
ENTRY_MIN_SECONDS_TO_CLOSE = 38.0
SCRATCH_SECONDS_TO_CLOSE = 34.0
FORCED_EXIT_SECONDS_TO_CLOSE = 23.0

TAIL_MIN_ASK_PRICE = 0.70
TAIL_MAX_ASK_PRICE = 0.86
EMERGENCY_MIN_ASK_PRICE = 0.60  # Dynamic fallback if cash < $3.80

MAX_SPREAD_USD = 0.025
MAX_OVERPAY_VS_FAIR = 0.015
MIN_FAIR_PROB = 0.71

TARGET_PROFIT_PER_SHARE_USD = 0.022
MIN_PROFIT_FLOOR_USD = 0.11
STOP_LOSS_PER_SHARE_USD = 0.085
SCRATCH_MAX_LOSS_PER_SHARE_USD = -0.006

BTC_BASE_VOL_5M = 68.0
MAX_CYCLES_PER_MARKET = 3


def _buy_fee_mult(price: float) -> float:
    return 1.01 + 0.07 * (1.0 - price)


def _sell_fee_mult(price: float) -> float:
    return 1.0 - (0.01 + 0.07 * (1.0 - price))


def _fair_prob_yes(btc_mid: float, opening_target: float, seconds_to_close: float) -> float:
    gap = btc_mid - opening_target
    sigma = BTC_BASE_VOL_5M * math.sqrt(max(seconds_to_close, 4.0) / 300.0)
    z = gap / max(sigma, 1.0)
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


class ApexBot:
    def __init__(self) -> None:
        self.last_market_id: str | None = None
        self.gap_history: deque[float] = deque(maxlen=8)
        self.market_flips: int = 0
        self.market_entries: int = 0
        self.was_in_position: bool = False
        self.loss_in_market: bool = False

    def _reset_market(self, market_id: str) -> None:
        self.last_market_id = market_id
        self.gap_history.clear()
        self.market_flips = 0
        self.market_entries = 0
        self.was_in_position = False
        self.loss_in_market = False

    def decide(self, obs: dict[str, Any]) -> dict[str, Any]:
        market = obs.get("market", {})
        account = obs.get("account", {})
        books = obs.get("books", {})
        reference = obs.get("reference")

        market_id = str(market.get("id", ""))
        seconds = float(market.get("secondsToClose", 0.0))
        cash_usd = float(account.get("cashUsd", 0.0))
        volume_usd = float(account.get("eligibleVolumeUsd", 0.0))
        position = account.get("position")

        # 1. Permanent Qualification Lock: Once >= $1,000 and flat, lock cash forever
        if volume_usd >= 1000.0 and position is None:
            return {"action": "HOLD"}

        # 2. Track per-market state & strike flips
        if market_id != self.last_market_id:
            self._reset_market(market_id)

        if position is not None and not self.was_in_position:
            self.market_entries += 1
        self.was_in_position = position is not None

        if not isinstance(reference, dict) or reference.get("targetProvisional", False):
            if position is not None and seconds <= FORCED_EXIT_SECONDS_TO_CLOSE:
                return {"action": "SELL"}
            return {"action": "HOLD"}

        btc_mid = float(reference.get("btcMidUsd", 0.0))
        target_usd = float(reference.get("openingTargetUsd", 0.0))
        current_gap = btc_mid - target_usd

        if self.gap_history:
            prev_gap = self.gap_history[-1]
            if (prev_gap > 2.0 and current_gap < -2.0) or (prev_gap < -2.0 and current_gap > 2.0):
                self.market_flips += 1
        self.gap_history.append(current_gap)

        # 3. Position Exit Engine (4-Tier Fee-Aware + Stop-Loss)
        if position is not None:
            side = str(position.get("outcome", "YES"))
            shares = float(position.get("shares", 0.0))
            paid_usd = float(position.get("buy_gross_usd", 0.0)) + float(position.get("buy_fees_usd", 0.0))

            bids = books.get(side, {}).get("bids", [])
            if not bids:
                return {"action": "SELL"} if seconds <= FORCED_EXIT_SECONDS_TO_CLOSE else {"action": "HOLD"}

            best_bid = float(bids[0][0])
            net_proceeds = shares * best_bid * _sell_fee_mult(best_bid)
            net_pnl = net_proceeds - paid_usd

            # Tier A: Rapid Scalp Take-Profit
            req_profit = max(TARGET_PROFIT_PER_SHARE_USD * shares, MIN_PROFIT_FLOOR_USD)
            if net_pnl >= req_profit:
                return {"action": "SELL"}

            # Tier B: Pre-Freeze Hard Deadline Exit
            if seconds <= FORCED_EXIT_SECONDS_TO_CLOSE:
                if net_pnl < 0:
                    self.loss_in_market = True
                return {"action": "SELL"}

            # Tier C: Soft Scratch Near Expiry
            if seconds <= SCRATCH_SECONDS_TO_CLOSE and net_pnl >= SCRATCH_MAX_LOSS_PER_SHARE_USD * shares:
                return {"action": "SELL"}

            # Tier D: Buffered Thesis Invalidation (ignores minor $1-$4 noise around strike)
            if (side == "YES" and current_gap < -8.0) or (side == "NO" and current_gap > 8.0):
                self.loss_in_market = True
                return {"action": "SELL"}

            # Tier E: Hard Stop-Loss (Prevents falling knives from wiping capital)
            if net_pnl <= -STOP_LOSS_PER_SHARE_USD * shares:
                self.loss_in_market = True
                return {"action": "SELL"}

            return {"action": "HOLD"}

        # 4. Tail-Zone Entry Engine
        if not (ENTRY_MIN_SECONDS_TO_CLOSE <= seconds <= ENTRY_MAX_SECONDS_TO_CLOSE):
            return {"action": "HOLD"}

        if self.loss_in_market or self.market_flips >= 2 or self.market_entries >= MAX_CYCLES_PER_MARKET:
            return {"action": "HOLD"}

        # Require meaningful BTC displacement and non-collapsing momentum
        if abs(current_gap) < 32.0:
            return {"action": "HOLD"}
        if len(self.gap_history) >= 2 and (abs(current_gap) - abs(self.gap_history[-2])) < -4.0:
            return {"action": "HOLD"}

        p_yes = _fair_prob_yes(btc_mid, target_usd, seconds)
        side = "YES" if current_gap > 0 else "NO"
        fair_p = p_yes if side == "YES" else (1.0 - p_yes)

        if fair_p < MIN_FAIR_PROB:
            return {"action": "HOLD"}

        side_book = books.get(side, {})
        asks = side_book.get("asks", [])
        bids = side_book.get("bids", [])
        if not asks or not bids:
            return {"action": "HOLD"}

        best_ask = float(asks[0][0])
        best_bid = float(bids[0][0])
        spread = best_ask - best_bid
        min_order_size = float(side_book.get("minOrderSize", 5.0))

        # Dynamic floor prevents sub-$4.00 capital starvation lock
        min_ask_allowed = EMERGENCY_MIN_ASK_PRICE if cash_usd < 3.80 else TAIL_MIN_ASK_PRICE
        if not (min_ask_allowed <= best_ask <= TAIL_MAX_ASK_PRICE):
            return {"action": "HOLD"}

        if spread > MAX_SPREAD_USD or best_ask > (fair_p + MAX_OVERPAY_VS_FAIR):
            return {"action": "HOLD"}

        # Verify L2 depth on top 2 levels so 250ms delay doesn't sweep empty book
        top2_ask_shares = sum(float(a[1]) for a in asks[:2] if float(a[0]) <= best_ask + 0.01)
        top2_bid_shares = sum(float(b[1]) for b in bids[:2] if float(b[0]) >= best_bid - 0.01)
        if top2_ask_shares < min_order_size * 1.1 or top2_bid_shares < min_order_size:
            return {"action": "HOLD"}

        # 5. Anti-Dust & Endgame Snipe Sizing (Guaranteed >= minOrderSize)
        unit_cost_with_fee = best_ask * _buy_fee_mult(best_ask)
        min_lot_cash = min_order_size * unit_cost_with_fee + 0.04

        spendable_cash = min(MAX_BUY_CASH_USD, cash_usd - CASH_SAFETY_BUFFER_USD)
        if spendable_cash < min_lot_cash:
            return {"action": "HOLD"}

        rem_vol = max(0.0, 1000.0 - volume_usd)
        if rem_vol < 9.50:
            # Endgame snipe: risk only enough to cross $1,000, floored at min_lot_cash
            spend_cash = min(spendable_cash, max(min_lot_cash, rem_vol * 0.56))
        else:
            spend_cash = spendable_cash

        # Cap by available top-of-book liquidity to prevent partial-fill slippage
        max_liquidity_cash = top2_ask_shares * unit_cost_with_fee * 0.90
        final_max_cash = round(min(spend_cash, max_liquidity_cash), 2)

        if final_max_cash < min_lot_cash - 0.02:
            return {"action": "HOLD"}

        return {
            "action": "BUY",
            "outcome": side,
            "maxCashUsd": final_max_cash,
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
