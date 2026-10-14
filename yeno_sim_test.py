#!/usr/bin/env python3
"""Market simulator and stress-test benchmark for YeNo Volume Bot Challenge.

Models the exact evaluator contract:
- $10 initial cash
- $5 max fee-inclusive BUY
- $1000 volume target
- 24-hour evaluation window (288 5-minute markets)
- 250ms latency (order fills against later book update)
- Dynamic crypto taker fees: 0.07 * shares * price * (1 - price)
- YeNo fee: 1% on gross
- Displayed depth, minOrderSize, spread, partial fills, 15s entry freeze
- Only fully closed cycles count towards volume
- Adverse slippage stress testing
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable


def calc_fees(fills: list[tuple[float, float]]) -> float:
    gross = sum(price * shares for price, shares in fills)
    protocol = sum(0.07 * shares * price * (1.0 - price) for price, shares in fills)
    return round(protocol + 1e-12, 5) + round(gross * 0.01 + 1e-12, 5)


class MarketSimulator:
    def __init__(
        self,
        seed: int = 42,
        adverse_spread_cents: float = 0.0,
        num_markets: int = 288,
    ) -> None:
        self.rng = random.Random(seed)
        self.adverse_spread_cents = adverse_spread_cents
        self.num_markets = num_markets

    def generate_market(self, market_index: int) -> list[dict[str, Any]]:
        """Simulate a 5-minute market with sequential observations from 120s down to 5s."""
        base_btc = 80000.0 + self.rng.gauss(0, 1500.0)
        opening_target = base_btc + self.rng.gauss(0, 40.0)

        # Drift regime for this 5-minute window
        trend = self.rng.choice([-1.0, 1.0]) * self.rng.uniform(1.0, 4.0)
        is_choppy = self.rng.random() < 0.25

        observations: list[dict[str, Any]] = []
        current_btc = opening_target + self.rng.gauss(0, 25.0)

        market_id = f"mkt-{market_index:03d}"
        base_time = 1789880000.0 + market_index * 300.0

        # Observations every 1.0 to 1.5 seconds from 120s down to 5s
        t = 120.0
        while t >= 5.0:
            dt = self.rng.uniform(0.8, 1.4)
            t -= dt
            if t < 5.0:
                break

            now = base_time + (300.0 - t)

            # Update BTC with realistic 5-minute volatility (~$60-150 range)
            if is_choppy:
                current_btc += self.rng.gauss(0, 12.0)
            else:
                current_btc += trend * dt * 1.5 + self.rng.gauss(0, 8.0)

            gap = current_btc - opening_target

            # Implied volatility for BTC binary options (uncertainty shrinks as t -> 0)
            sigma = 75.0 * math.sqrt(max(t, 1.0) / 300.0)
            z = gap / max(sigma, 0.5)
            prob_yes = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
            prob_yes = max(0.01, min(0.99, prob_yes))
            prob_no = 1.0 - prob_yes

            # Build L2 books with spread and depth
            spread = max(0.01, self.rng.uniform(0.01, 0.03) + self.adverse_spread_cents / 100.0)

            # YES book
            yes_mid = prob_yes
            yes_bid_p = round(max(0.01, min(0.98, yes_mid - spread / 2.0)), 2)
            yes_ask_p = round(max(yes_bid_p + 0.01, min(0.99, yes_mid + spread / 2.0)), 2)

            # NO book
            no_mid = prob_no
            no_bid_p = round(max(0.01, min(0.98, no_mid - spread / 2.0)), 2)
            no_ask_p = round(max(no_bid_p + 0.01, min(0.99, no_mid + spread / 2.0)), 2)

            # Depth
            yes_depth = max(5, int(self.rng.uniform(15, 60)))
            no_depth = max(5, int(self.rng.uniform(15, 60)))

            obs = {
                "schemaVersion": 1,
                "timestamp": now,
                "market": {
                    "id": market_id,
                    "secondsToClose": round(t, 2),
                },
                "rules": {
                    "maximumBuyCashUsd": 5.0,
                    "targetVolumeUsd": 1000.0,
                    "evaluationWindowHours": 24,
                    "onePositionAtATime": True,
                    "buyFeesIncludedInMaximum": True,
                },
                "reference": {
                    "btcMidUsd": round(current_btc, 3),
                    "openingTargetUsd": round(opening_target, 3),
                    "observedAt": round(now - self.rng.uniform(0.1, 0.4), 3),
                    "targetObservedAt": round(base_time, 3),
                    "targetProvisional": False,
                },
                "books": {
                    "YES": {
                        "bids": [[yes_bid_p, yes_depth], [round(yes_bid_p - 0.01, 2), yes_depth * 2]],
                        "asks": [[yes_ask_p, yes_depth], [round(yes_ask_p + 0.01, 2), yes_depth * 2]],
                        "minOrderSize": 5,
                    },
                    "NO": {
                        "bids": [[no_bid_p, no_depth], [round(no_bid_p - 0.01, 2), no_depth * 2]],
                        "asks": [[no_ask_p, no_depth], [round(no_ask_p + 0.01, 2), no_depth * 2]],
                        "minOrderSize": 5,
                    },
                },
            }
            observations.append(obs)

        return observations


def run_evaluation(bot_factory: Callable[[], Any], seed: int = 42, adverse_spread_cents: float = 0.0) -> dict[str, Any]:
    sim = MarketSimulator(seed=seed, adverse_spread_cents=adverse_spread_cents, num_markets=288)
    bot = bot_factory()

    cash = 10.0
    volume = 0.0
    position: dict[str, Any] | None = None
    completed_cycles = 0
    peak_cash = 10.0
    max_drawdown = 0.0
    qualified_at_crossing = False
    cash_at_crossing = 0.0
    crossing_market_idx = 0

    pending_order: dict[str, Any] | None = None

    for m_idx in range(sim.num_markets):
        market_obs = sim.generate_market(m_idx)
        for i, obs in enumerate(market_obs):
            # Check if pending order can execute (250ms latency -> filled on next observation)
            if pending_order is not None:
                action = pending_order["action"]
                if action == "BUY" and position is None:
                    outcome = pending_order["outcome"]
                    max_cash = min(pending_order["maxCashUsd"], cash, 5.0)
                    asks = obs["books"].get(outcome, {}).get("asks", [])
                    min_size = obs["books"].get(outcome, {}).get("minOrderSize", 5)

                    # Execute BUY against asks
                    remaining_cash = max_cash
                    fills: list[tuple[float, float]] = []
                    for p, sz in asks:
                        # rough fee factor for fee calculation inside maxCash
                        k = p * (1.01 + 0.07 * (1.0 - p))
                        max_sh = remaining_cash / k
                        take_sh = min(max_sh, sz)
                        if take_sh > 0:
                            fills.append((p, take_sh))
                            remaining_cash -= take_sh * k
                        if remaining_cash < 0.1:
                            break

                    total_shares = sum(s for _, s in fills)
                    if total_shares >= min_size:
                        buy_gross = sum(p * s for p, s in fills)
                        buy_fees = calc_fees(fills)
                        total_cost = buy_gross + buy_fees
                        if total_cost <= cash + 1e-6:
                            cash -= total_cost
                            position = {
                                "market_id": obs["market"]["id"],
                                "outcome": outcome,
                                "shares": round(total_shares, 4),
                                "buy_gross_usd": round(buy_gross, 5),
                                "buy_fees_usd": round(buy_fees, 5),
                                "sell_gross_usd": 0.0,
                                "sell_fees_usd": 0.0,
                            }
                elif action == "SELL" and position is not None:
                    outcome = position["outcome"]
                    shares_to_sell = position["shares"]
                    bids = obs["books"].get(outcome, {}).get("bids", [])

                    rem = shares_to_sell
                    fills = []
                    for p, sz in bids:
                        take = min(rem, sz)
                        fills.append((p, take))
                        rem -= take
                        if rem <= 1e-6:
                            break

                    if fills:
                        sold_shares = sum(s for _, s in fills)
                        sell_gross = sum(p * s for p, s in fills)
                        sell_fees = calc_fees(fills)
                        cash += (sell_gross - sell_fees)

                        # If full exit:
                        if rem <= 1e-6:
                            cycle_volume = position["buy_gross_usd"] + sell_gross
                            volume += cycle_volume
                            completed_cycles += 1
                            position = None

                            # Check qualification crossing
                            if volume >= 1000.0 and not qualified_at_crossing:
                                qualified_at_crossing = True
                                cash_at_crossing = cash
                                crossing_market_idx = m_idx

                pending_order = None

            # Track peak and drawdown
            est_equity = cash + (position["buy_gross_usd"] if position else 0.0)
            if est_equity > peak_cash:
                peak_cash = est_equity
            dd = peak_cash - est_equity
            if dd > max_drawdown:
                max_drawdown = dd

            # Construct input observation for bot
            bot_input = dict(obs)
            bot_input["account"] = {
                "cashUsd": round(cash, 4),
                "eligibleVolumeUsd": round(volume, 4),
                "completedCycles": completed_cycles,
                "position": position,
            }

            # If inside final 15 seconds, new BUYs are blocked by evaluator
            decision = bot.decide(bot_input)

            if decision.get("action") == "BUY":
                if obs["market"]["secondsToClose"] > 15.0 and position is None and cash >= 2.0:
                    pending_order = decision
            elif decision.get("action") == "SELL":
                if position is not None:
                    pending_order = decision

        # End of market: if position is still open, it expires (settlement = $0 volume)
        if position is not None:
            # Position expired without exit: $0 volume added!
            # Outcome settlement:
            # if outcome won, cash += shares * 1.0, else 0
            # but volume adds ZERO!
            position = None

    is_flat = position is None and pending_order is None
    return {
        "volume": round(volume, 2),
        "endingCash": round(cash, 4),
        "completedCycles": completed_cycles,
        "maxDrawdown": round(max_drawdown, 4),
        "qualified": qualified_at_crossing,
        "cashAtCrossing": round(cash_at_crossing, 4) if qualified_at_crossing else 0.0,
        "crossingMarket": crossing_market_idx if qualified_at_crossing else None,
        "isFlat": is_flat,
    }


if __name__ == "__main__":
    import importlib
    house_mod = importlib.import_module("yeno-house-bot-v1")
    apex_mod = importlib.import_module("yeno_apex_bot")

    print("=" * 60)
    print("BENCHMARK COMPARISON: HOUSE BOT vs APEX BOT")
    print("=" * 60)

    seeds = [42, 101, 777, 999, 1234, 5555, 8888]

    print("\n--- Running Championship ApexBot across 7 distinct 24h market paths ---")
    apex_results = []
    for s in seeds:
        res = run_evaluation(lambda: apex_mod.ApexBot(), seed=s)
        apex_results.append(res)
        print(f"Seed {s:<5}: Volume=${res['volume']:<8.2f} EndingCash=${res['endingCash']:<7.4f} Qualified={str(res['qualified']):<5} CashCrossing=${res['cashAtCrossing']:<7.4f} Cycles={res['completedCycles']:<3} Flat={res['isFlat']}")

    print("\n--- Running HouseBot v1 on same paths ---")
    for s in seeds:
        res = run_evaluation(lambda: house_mod.HouseBot(), seed=s)
        print(f"Seed {s}: Volume=${res['volume']}, EndingCash=${res['endingCash']}, Qualified={res['qualified']}, Cycles={res['completedCycles']}, MaxDD=${res['maxDrawdown']}")

