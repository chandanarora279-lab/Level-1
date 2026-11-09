"""Offline check of ATM selection using fake OptionContracts (no network)."""

from decimal import Decimal

import pandas as pd

from nautilus_trader.model.currencies import BTC
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from straddle.atm import filter_btc_options
from straddle.atm import select_atm_straddle

NOW = pd.Timestamp("2026-11-08", tz="UTC").value


def make_option(expiry: str, strike: int, kind: OptionKind) -> OptionContract:
    exp = pd.Timestamp(expiry, tz="UTC")
    letter = "C" if kind == OptionKind.CALL else "P"
    sym = f"BTC-{exp:%d%b%y}".upper() + f"-{strike}-{letter}-USDT-OPTION"
    return OptionContract(
        instrument_id=InstrumentId.from_str(f"{sym}.BYBIT"),
        raw_symbol=Symbol(sym),
        asset_class=AssetClass.CRYPTOCURRENCY,
        currency=USDT,
        price_precision=1,
        price_increment=Price.from_str("5.0"),
        multiplier=Quantity.from_int(1),
        lot_size=Quantity.from_str("0.01"),
        underlying="BTC",
        option_kind=kind,
        strike_price=Price.from_int(strike),
        activation_ns=0,
        expiration_ns=exp.value,
        ts_event=0,
        ts_init=0,
    )


def build_chain():
    opts = []
    for expiry in ("2026-11-08 08:00", "2026-11-09 08:00", "2026-11-13 08:00"):
        for strike in (74000, 75000, 76000, 77000):
            opts += [make_option(expiry, strike, OptionKind.CALL), make_option(expiry, strike, OptionKind.PUT)]
    opts.append(make_option("2026-11-09 08:00", 75500, OptionKind.CALL))  # call without matching put
    return opts


def test_picks_nearest_valid_expiry_and_strike():
    sel = select_atm_straddle(build_chain(), reference_price=75_620.0, now_ns=NOW + 9 * 3600 * 10**9)
    assert sel is not None
    assert sel.strike == 76000  # 75500 has no put, so it is ignored
    assert sel.call.option_kind == OptionKind.CALL and sel.put.option_kind == OptionKind.PUT
    assert sel.call.expiration_ns == sel.put.expiration_ns
    assert sel.call.expiration_utc == pd.Timestamp("2026-11-09 08:00", tz="UTC")  # 8 Nov expired


def test_expiry_index_and_filter():
    opts = filter_btc_options(build_chain(), quote_currency="USDT")
    sel = select_atm_straddle(opts, reference_price=74_400.0, now_ns=NOW, expiry_index=2)
    assert sel.strike == 74000
    assert sel.call.expiration_utc == pd.Timestamp("2026-11-13 08:00", tz="UTC")
    assert filter_btc_options(opts, quote_currency="USDC") == []
    assert sel.call.lot_size == Quantity.from_str("0.01") and Decimal("0.01") > 0
