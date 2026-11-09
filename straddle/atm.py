"""
Pure helper logic for picking an ATM straddle out of a list of option instruments.

Kept free of any NautilusTrader runtime state (no cache, no clock) so it can be
unit-tested offline - see tests/test_atm.py.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from nautilus_trader.model.enums import InstrumentClass
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.model.instruments import OptionContract


Option = CryptoOption | OptionContract


@dataclass(frozen=True)
class StraddleSelection:
    call: Option
    put: Option
    strike: float
    expiration_ns: int
    reference_price: float

    def describe(self) -> str:
        expiry = self.call.expiration_utc.strftime("%Y-%m-%d %H:%M UTC")
        diff = self.strike - self.reference_price
        return (
            f"ATM strike={self.strike:,.0f} (ref={self.reference_price:,.2f}, "
            f"diff={diff:+,.2f}) expiry={expiry} "
            f"CALL={self.call.id} PUT={self.put.id}"
        )


def filter_btc_options(
    instruments: list,
    base_coin: str = "BTC",
    quote_currency: str | None = "USDT",
) -> list[Option]:
    """Keep only option contracts on `base_coin` (optionally settled in `quote_currency`)."""
    result = []
    for inst in instruments:
        # Bybit options load as CryptoOption; OptionContract also accepted.
        if inst.instrument_class != InstrumentClass.OPTION:
            continue
        if not inst.id.symbol.value.startswith(f"{base_coin}-"):
            continue
        if quote_currency and inst.quote_currency.code != quote_currency:
            continue
        result.append(inst)
    return result


def group_by_expiry(options: list[Option]) -> dict[int, dict[float, dict[OptionKind, Option]]]:
    """expiration_ns -> strike -> {CALL: contract, PUT: contract}."""
    chain: dict[int, dict[float, dict[OptionKind, Option]]] = defaultdict(lambda: defaultdict(dict))
    for opt in options:
        chain[opt.expiration_ns][float(opt.strike_price)][opt.option_kind] = opt
    return chain


def select_atm_straddle(
    options: list[Option],
    reference_price: float,
    now_ns: int,
    min_time_to_expiry_secs: int = 3600,
    expiry_index: int = 0,
) -> StraddleSelection | None:
    """
    Pick the Call + Put with the same expiry and the strike closest to `reference_price`.

    - Expiries closer than `min_time_to_expiry_secs` are skipped (avoid contracts
      that are about to settle).
    - `expiry_index` = 0 picks the nearest valid expiry, 1 the next one, etc.
    - Only strikes where BOTH a call and a put exist are considered.
    """
    if reference_price <= 0:
        return None

    chain = group_by_expiry(options)
    cutoff = now_ns + min_time_to_expiry_secs * 1_000_000_000

    valid_expiries = []
    for expiry_ns in sorted(chain):
        if expiry_ns <= cutoff:
            continue
        paired = [k for k, legs in chain[expiry_ns].items() if OptionKind.CALL in legs and OptionKind.PUT in legs]
        if paired:
            valid_expiries.append(expiry_ns)

    if len(valid_expiries) <= expiry_index:
        return None

    expiry_ns = valid_expiries[expiry_index]
    strikes = chain[expiry_ns]
    paired_strikes = [k for k, legs in strikes.items() if OptionKind.CALL in legs and OptionKind.PUT in legs]
    # Nearest strike to the reference price; ties broken by the lower strike.
    atm_strike = min(paired_strikes, key=lambda k: (abs(k - reference_price), k))

    return StraddleSelection(
        call=strikes[atm_strike][OptionKind.CALL],
        put=strikes[atm_strike][OptionKind.PUT],
        strike=atm_strike,
        expiration_ns=expiry_ns,
        reference_price=reference_price,
    )
