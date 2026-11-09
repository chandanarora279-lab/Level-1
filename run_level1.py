"""
Level 1 runner: builds a NautilusTrader TradingNode connected to Bybit and runs the
BTC ATM short-straddle strategy.

    .venv\\Scripts\\python.exe run_level1.py

Credentials are read from .env (see .env.example). Defaults to Bybit TESTNET.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import msgspec
from dotenv import load_dotenv

from nautilus_trader.adapters.bybit import BYBIT
from nautilus_trader.adapters.bybit import BybitDataClientConfig
from nautilus_trader.adapters.bybit import BybitEnvironment
from nautilus_trader.adapters.bybit import BybitExecClientConfig
from nautilus_trader.adapters.bybit import BybitLiveDataClientFactory
from nautilus_trader.adapters.bybit import BybitLiveExecClientFactory
from nautilus_trader.adapters.bybit import BybitProductType
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from straddle.strategy import ShortStraddleConfig
from straddle.strategy import ShortStraddleStrategy


def main() -> None:
    load_dotenv()

    env_name = os.getenv("BYBIT_ENVIRONMENT", "TESTNET").upper()
    environment = {
        "TESTNET": BybitEnvironment.TESTNET,
        "DEMO": BybitEnvironment.DEMO,
        "MAINNET": BybitEnvironment.MAINNET,
    }[env_name]
    if environment == BybitEnvironment.MAINNET and os.getenv("I_UNDERSTAND_THIS_IS_REAL_MONEY") != "yes":
        sys.exit("Refusing to run on MAINNET. Set I_UNDERSTAND_THIS_IS_REAL_MONEY=yes to override.")

    # Load only BTC instruments: LINEAR (BTCUSDT perp = reference price) + OPTION.
    provider = InstrumentProviderConfig(load_all=True, filters={"base_coin": "BTC"})

    config = TradingNodeConfig(
        trader_id=TraderId("STRADDLE-001"),
        logging=LoggingConfig(
            log_level="INFO",
            log_level_file="DEBUG",
            log_directory="logs",
            log_colors=True,
        ),
        exec_engine=LiveExecEngineConfig(
            # On startup, reconcile local state with orders/positions already on Bybit.
            reconciliation=True,
            reconciliation_lookback_mins=1440,
        ),
        data_clients={
            BYBIT: BybitDataClientConfig(
                api_key=None,  # -> BYBIT_TESTNET_API_KEY / BYBIT_API_KEY env vars
                api_secret=None,
                environment=environment,
                product_types=(BybitProductType.LINEAR, BybitProductType.OPTION),
                instrument_provider=provider,
            ),
        },
        exec_clients={
            BYBIT: BybitExecClientConfig(
                api_key=None,
                api_secret=None,
                environment=environment,
                product_types=(BybitProductType.OPTION,),
                instrument_provider=provider,
            ),
        },
        timeout_connection=60.0,
        timeout_reconciliation=20.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
    )

    dry_run = os.getenv("DRY_RUN", "0") == "1"
    if dry_run:
        # Public market data needs no API key; drop the exec client entirely.
        config = msgspec.structs.replace(config, exec_clients={})

    node = TradingNode(config=config)

    strategy = ShortStraddleStrategy(
        ShortStraddleConfig(
            quantity=Decimal(os.getenv("STRADDLE_QTY", "0.01")),
            expiry_index=int(os.getenv("STRADDLE_EXPIRY_INDEX", "0")),
            order_type=os.getenv("STRADDLE_ORDER_TYPE", "LIMIT"),
            option_quote_currency=os.getenv("STRADDLE_QUOTE_CCY", "USDT"),
            dry_run=dry_run,
        ),
    )
    node.trader.add_strategy(strategy)

    node.add_data_client_factory(BYBIT, BybitLiveDataClientFactory)
    if not dry_run:
        node.add_exec_client_factory(BYBIT, BybitLiveExecClientFactory)
    node.build()

    try:
        node.run()  # blocks; Ctrl+C to stop
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
