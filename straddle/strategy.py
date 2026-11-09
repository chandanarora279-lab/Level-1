"""
Level 1 - BTC ATM short straddle (naked: no SL / TP / automated exits).

Execution flow
--------------
on_start
  -> read every BTC option the Bybit instrument provider loaded into the cache
  -> subscribe to the BTC reference price (index price + quotes of BTCUSDT perp)
on_index_price / on_quote_tick (reference)
  -> once a reference price is known: pick the nearest ATM strike for the chosen
     expiry, then subscribe to quotes (+ greeks) of the Call and the Put
on_quote_tick / on_mark_price (option legs)
  -> once BOTH legs are priceable (bid, else mark): submit one SELL limit per leg (only once)
on_order_* / on_position_* events
  -> log acknowledgements, rejections, fills and position changes
timer (every `status_interval_secs`)
  -> print a status table: reference price, leg quotes, orders, positions, PnL
"""

from __future__ import annotations

import math
from decimal import Decimal

import pandas as pd

from nautilus_trader.adapters.bybit import BYBIT_VENUE
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import IndexPriceUpdate
from nautilus_trader.model.data import MarkPriceUpdate
from nautilus_trader.model.data import OptionGreeks
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderAccepted
from nautilus_trader.model.events import OrderCanceled
from nautilus_trader.model.events import OrderDenied
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.events import OrderSubmitted
from nautilus_trader.model.events import PositionChanged
from nautilus_trader.model.events import PositionClosed
from nautilus_trader.model.events import PositionOpened
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price
from nautilus_trader.trading.strategy import Strategy

from straddle.atm import Option
from straddle.atm import StraddleSelection
from straddle.atm import filter_btc_options
from straddle.atm import group_by_expiry
from straddle.atm import select_atm_straddle


class ShortStraddleConfig(StrategyConfig, frozen=True):
    # Underlying reference: BTCUSDT perpetual. Its ticker carries the BTC index price.
    reference_instrument_id: str = "BTCUSDT-LINEAR.BYBIT"
    base_coin: str = "BTC"
    option_quote_currency: str = "USDT"  # Bybit lists USDT- and USDC-settled BTC options
    # Contracts per leg (same quantity on both legs). Bybit BTC option min = 0.01.
    quantity: Decimal = Decimal("0.01")
    expiry_index: int = 0  # 0 = nearest expiry, 1 = next, ...
    min_time_to_expiry_secs: int = 6 * 3600  # skip expiries settling within 6h
    # "LIMIT": sell at the best bid (marketable). If a leg has NO bid (common on
    #          testnet), fall back to a resting limit at the leg's mark price.
    # "MARKET": market order (needs a bid to fill).
    order_type: str = "LIMIT"
    status_interval_secs: int = 15
    # Market-data only: log the orders that WOULD be sent, submit nothing.
    dry_run: bool = False


class ShortStraddleStrategy(Strategy):
    def __init__(self, config: ShortStraddleConfig) -> None:
        super().__init__(config)
        self.ref_id = InstrumentId.from_str(config.reference_instrument_id)
        self.ref_index_price: float | None = None
        self.ref_mid_price: float | None = None

        self.selection: StraddleSelection | None = None
        self.leg_quotes: dict[InstrumentId, QuoteTick] = {}
        self.leg_greeks: dict[InstrumentId, OptionGreeks] = {}
        self.leg_marks: dict[InstrumentId, float] = {}
        self.entry_submitted = False
        self.options: list[Option] = []

    # ------------------------------------------------------------------ lifecycle

    def on_start(self) -> None:
        # Steps 3/4: the Bybit instrument provider already loaded all BTC instruments
        # (OPTION + LINEAR) into the cache when the data client connected.
        all_instruments = self.cache.instruments(venue=BYBIT_VENUE)
        self.options = filter_btc_options(
            all_instruments,
            base_coin=self.config.base_coin,
            quote_currency=self.config.option_quote_currency,
        )
        self._log_option_universe()

        if not self.options:
            self.log.error("No BTC options found in cache - check product_types / filters. Stopping.")
            self.stop()
            return

        if self.cache.instrument(self.ref_id) is None:
            self.log.error(f"Reference instrument {self.ref_id} not in cache. Stopping.")
            self.stop()
            return

        # Reference price feed. Index price is the cleanest ATM reference;
        # quotes (mid) are a fallback in case the index stream is slow.
        self.subscribe_index_prices(self.ref_id)
        self.subscribe_quote_ticks(self.ref_id)

        self.clock.set_timer(
            name="status",
            interval=pd.Timedelta(seconds=self.config.status_interval_secs),
            callback=self._on_status_timer,
        )

    def _on_status_timer(self, _event) -> None:
        # Fallback: if the index stream never arrived, select ATM off the perp mid.
        if self.selection is None and self.ref_index_price is None and self.ref_mid_price:
            self.log.warning("No index price yet - using BTCUSDT perp mid as ATM reference.")
            self._maybe_select_atm(allow_mid=True)
        self._maybe_enter()
        self._log_status()

    def on_stop(self) -> None:
        # Level 1: deliberately NO automated exit. Positions stay open on the venue.
        self.log.warning("Strategy stopping - open positions are NOT closed (Level 1: no exits).")
        self._log_status()

    # ------------------------------------------------------------------ market data

    def on_index_price(self, index_price: IndexPriceUpdate) -> None:
        if index_price.instrument_id == self.ref_id:
            self.ref_index_price = float(index_price.value)
            self._maybe_select_atm()

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if tick.instrument_id == self.ref_id:
            self.ref_mid_price = (float(tick.bid_price) + float(tick.ask_price)) / 2
            return

        if self.selection and tick.instrument_id in (self.selection.call.id, self.selection.put.id):
            self.leg_quotes[tick.instrument_id] = tick
            self._maybe_enter()

    def on_option_greeks(self, greeks: OptionGreeks) -> None:
        self.leg_greeks[greeks.instrument_id] = greeks

    def on_mark_price(self, mark: MarkPriceUpdate) -> None:
        self.leg_marks[mark.instrument_id] = float(mark.value)
        self._maybe_enter()

    # ------------------------------------------------------------------ ATM selection

    def _maybe_select_atm(self, allow_mid: bool = False) -> None:
        if self.selection is not None:
            return
        # Prefer the BTC index price (what Bybit options are priced off).
        ref = self.ref_index_price or (self.ref_mid_price if allow_mid else None)
        if ref is None:
            return

        # Steps 5/6: nearest strike to the reference, same strike + expiry for C and P.
        selection = select_atm_straddle(
            self.options,
            reference_price=ref,
            now_ns=self.clock.timestamp_ns(),
            min_time_to_expiry_secs=self.config.min_time_to_expiry_secs,
            expiry_index=self.config.expiry_index,
        )
        if selection is None:
            self.log.error("Could not find a matching Call/Put pair for any valid expiry.")
            return

        self.selection = selection
        self.log.info(f"Selected straddle: {selection.describe()}")

        for leg in (selection.call, selection.put):
            self.subscribe_quote_ticks(leg.id)
            self.subscribe_option_greeks(leg.id)
            self.subscribe_mark_prices(leg.id)

    # ------------------------------------------------------------------ entry

    def _sell_price(self, leg: Option) -> tuple[Price, str] | None:
        """Best bid if there is one, else the mark price rounded UP to the tick."""
        quote = self.leg_quotes.get(leg.id)
        if quote is not None and quote.bid_price.as_double() > 0:
            return quote.bid_price, "bid"
        mark = self.leg_marks.get(leg.id)
        if mark:
            tick = leg.price_increment.as_double()
            return leg.make_price(max(math.ceil(mark / tick) * tick, tick)), "mark"
        return None

    def _maybe_enter(self) -> None:
        if self.entry_submitted or self.selection is None:
            return
        legs = (self.selection.call, self.selection.put)
        prices = [self._sell_price(leg) for leg in legs]
        if any(p is None for p in prices):
            return  # wait until BOTH legs have a bid or a mark price
        market = self.config.order_type.upper() == "MARKET"
        if market and any(src != "bid" for _, src in prices):
            self.log.warning("MARKET mode but a leg has no bid - waiting.")
            return

        # Step 7: one SELL per leg, identical quantity, submitted together.
        self.entry_submitted = True
        premium = sum(p.as_double() for p, _ in prices)
        self.log.info(
            f"Entry: CALL @ {prices[0][0]} ({prices[0][1]}), PUT @ {prices[1][0]} ({prices[1][1]}), "
            f"combined premium/contract={premium:,.2f}",
        )
        if self.config.dry_run:
            self.log.warning("[DRY RUN] orders NOT submitted.")
            return

        for leg, (price, _src) in zip(legs, prices):
            qty = leg.make_qty(self.config.quantity)
            if market:
                order = self.order_factory.market(
                    instrument_id=leg.id,
                    order_side=OrderSide.SELL,
                    quantity=qty,
                    time_in_force=TimeInForce.IOC,
                )
            else:
                order = self.order_factory.limit(
                    instrument_id=leg.id,
                    order_side=OrderSide.SELL,
                    quantity=qty,
                    price=price,
                    time_in_force=TimeInForce.GTC,
                )
            self.log.info(f"Submitting {order}")
            self.submit_order(order)

    # ------------------------------------------------------------------ order / position events (step 8)

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        self.log.info(f"[ORDER SUBMITTED] {event.client_order_id} {event.instrument_id}")

    def on_order_accepted(self, event: OrderAccepted) -> None:
        self.log.info(f"[ORDER ACCEPTED] {event.client_order_id} venue_id={event.venue_order_id}")

    def on_order_rejected(self, event: OrderRejected) -> None:
        self.log.error(f"[ORDER REJECTED] {event.client_order_id}: {event.reason}")

    def on_order_denied(self, event: OrderDenied) -> None:
        self.log.error(f"[ORDER DENIED] {event.client_order_id}: {event.reason}")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        self.log.warning(f"[ORDER CANCELED] {event.client_order_id}")

    def on_order_filled(self, event: OrderFilled) -> None:
        order = self.cache.order(event.client_order_id)
        self.log.info(
            f"[ORDER FILLED] {event.instrument_id} {event.order_side.name} "
            f"{event.last_qty} @ {event.last_px} "
            f"(filled {order.filled_qty}/{order.quantity}, leaves {order.leaves_qty}) "
            f"commission={event.commission}",
        )

    def on_position_opened(self, event: PositionOpened) -> None:
        self.log.info(f"[POSITION OPENED] {event.instrument_id} {event.side.name} qty={event.quantity} avg={event.avg_px_open}")

    def on_position_changed(self, event: PositionChanged) -> None:
        self.log.info(f"[POSITION CHANGED] {event.instrument_id} {event.side.name} qty={event.quantity} avg={event.avg_px_open}")

    def on_position_closed(self, event: PositionClosed) -> None:
        self.log.info(f"[POSITION CLOSED] {event.instrument_id} realized_pnl={event.realized_pnl}")

    # ------------------------------------------------------------------ reporting

    def _log_option_universe(self) -> None:
        chain = group_by_expiry(self.options)
        calls = sum(1 for o in self.options if o.option_kind == OptionKind.CALL)
        puts = len(self.options) - calls
        self.log.info(f"Found {len(self.options)} BTC options ({calls} calls / {puts} puts) across {len(chain)} expiries")
        for expiry_ns in sorted(chain)[:5]:
            strikes = sorted(chain[expiry_ns])
            sample = next(iter(chain[expiry_ns][strikes[0]].values()))
            self.log.info(
                f"  expiry {sample.expiration_utc:%Y-%m-%d}: {len(strikes)} strikes "
                f"[{strikes[0]:,.0f} .. {strikes[-1]:,.0f}]",
            )

    def _log_status(self) -> None:
        lines = [f"---- STATUS ---- ref index={self.ref_index_price} mid={self.ref_mid_price}"]
        if self.selection is None:
            lines.append("  no straddle selected yet")
            self.log.info("\n".join(lines))
            return

        combined_bid = combined_ask = 0.0
        for leg in (self.selection.call, self.selection.put):
            q = self.leg_quotes.get(leg.id)
            g = self.leg_greeks.get(leg.id)
            if q:
                combined_bid += q.bid_price.as_double()
                combined_ask += q.ask_price.as_double()
            greeks = f" delta={g.delta:+.3f} iv={g.mark_iv}" if g else ""
            mark = self.leg_marks.get(leg.id)
            lines.append(
                f"  {leg.id}: bid={q.bid_price if q else '-'} ask={q.ask_price if q else '-'} "
                f"mark={mark if mark is not None else '-'}{greeks}",
            )
        lines.append(f"  straddle per contract: bid={combined_bid:,.2f} ask(buyback)={combined_ask:,.2f}")

        for order in self.cache.orders(strategy_id=self.id):
            lines.append(
                f"  order {order.client_order_id} {order.instrument_id.symbol} {order.side.name} "
                f"{order.status.name} filled={order.filled_qty}/{order.quantity} avg_px={order.avg_px}",
            )

        for pos in self.cache.positions_open(strategy_id=self.id):
            q = self.leg_quotes.get(pos.instrument_id)
            # Short position -> value it at the cost to buy back: ask, else mark.
            if q is not None and q.ask_price.as_double() > 0:
                upnl = pos.unrealized_pnl(q.ask_price)
            elif pos.instrument_id in self.leg_marks:
                inst = self.cache.instrument(pos.instrument_id)
                upnl = pos.unrealized_pnl(inst.make_price(self.leg_marks[pos.instrument_id]))
            else:
                upnl = None
            lines.append(
                f"  position {pos.instrument_id.symbol} {pos.side.name} qty={pos.quantity} "
                f"entry={pos.avg_px_open} uPnL={upnl}",
            )
        self.log.info("\n".join(lines))

