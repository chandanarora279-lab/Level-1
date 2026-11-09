# Level 1 – BTC ATM Short Straddle on Bybit (NautilusTrader)

Sells 1 ATM BTC Call + 1 ATM BTC Put (same strike, same expiry) on **Bybit Testnet**
through NautilusTrader, then tracks order acks, fills and positions. Per the Level 1 spec,
it has **no stop-loss, no take-profit and no automatic exits**.

```
G:\Level-1
├── run_level1.py          # builds the TradingNode (Bybit data + exec clients) and runs it
├── straddle/
│   ├── atm.py             # pure logic: filter BTC options, group chain, pick ATM Call/Put
│   └── strategy.py        # the Nautilus Strategy (data -> ATM -> sell -> track)
├── tests/test_atm.py      # offline unit tests for the ATM selection
├── .env.example           # copy to .env and add your testnet API key
└── logs/                  # Nautilus log files (DEBUG level)
```

## Setup

1. Python 3.13 venv with `nautilus_trader==1.231.0` is already in `.venv`. To recreate it:
   ```bash
   python -m venv .venv
   ```
   ```bash
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. Create a Bybit **testnet** account at https://testnet.bybit.com, get test USDT from the faucet
   (Assets → Request test coins), and create an API key (API Management → System-generated,
   Read-Write, enable *Orders* and *Positions* for Unified Trading).
3. Copy `.env.example` to `.env` and paste the key/secret into `BYBIT_TESTNET_API_KEY` / `BYBIT_TESTNET_API_SECRET`.

## Run

Dry run (public market data only, no API key needed, no orders are sent):
```bash
$env:DRY_RUN="1"; .venv\Scripts\python.exe run_level1.py
```
Live on testnet (sends the two sell orders):
```bash
$env:DRY_RUN="0"; .venv\Scripts\python.exe run_level1.py
```
Stop with `Ctrl+C`. Positions remain open on Bybit (no exits in Level 1).

Unit tests:
```bash
$env:PYTHONPATH="."; .venv\Scripts\python.exe -m pytest -q tests
```

Knobs (in `.env`): `STRADDLE_QTY` (default 0.01 = Bybit minimum), `STRADDLE_EXPIRY_INDEX`
(0 = nearest expiry at least 6h away, 1 = next…), `STRADDLE_ORDER_TYPE` (`LIMIT`/`MARKET`).

### Testnet liquidity note
Testnet option books are mostly empty (many strikes show bid = 0). So:
- `LIMIT` mode sells at the **best bid** if there is one, otherwise places a **resting limit at the
  mark price** (rounded up to the tick). You will see `ACCEPTED` quickly; a fill only happens if
  someone on testnet takes the other side.
- To get fills, pick an expiry that has bids (e.g. a monthly like 25DEC26) via `STRADDLE_EXPIRY_INDEX`,
  or check bids at https://testnet.bybit.com/trade/option/usdt/BTC.

## Execution flow (walkthrough notes)

**1. How does NautilusTrader start?** `run_level1.py` builds a `TradingNodeConfig` → `TradingNode`.
`node.build()` uses the registered factories (`BybitLiveDataClientFactory`, `BybitLiveExecClientFactory`)
to create the clients. `node.run()` starts the event loop, connects the clients, the instrument
provider loads instruments into the **Cache**, the exec engine **reconciles** existing orders/positions
with Bybit, and then the Strategy's `on_start()` is called.

**2. Bybit configuration.** `BybitDataClientConfig` with `product_types=(LINEAR, OPTION)` (LINEAR for
the BTCUSDT perp = reference price, OPTION for the options) and `BybitExecClientConfig` with
`product_types=(OPTION,)`. `environment=TESTNET`. `InstrumentProviderConfig(load_all=True,
filters={"base_coin": "BTC"})` loads only BTC instruments. API keys come from env vars
`BYBIT_TESTNET_API_KEY/SECRET` (config leaves `api_key=None`, the factory reads them).

**3. How market data arrives.** The data client opens Bybit public WebSockets (one per product
type). Rust parses the ticker/orderbook messages into Nautilus objects (`QuoteTick`,
`IndexPriceUpdate`, `MarkPriceUpdate`, `OptionGreeks`) → DataEngine → MessageBus → the strategy's
handlers.

**4. Identifying / subscribing to BTC options.** In `on_start`, `cache.instruments(venue=BYBIT)` is
filtered to `instrument_class == OPTION`, symbol starting `BTC-` and USDT-settled
(Bybit options load as `CryptoOption`, e.g. `BTC-9NOV26-75750-C-USDT-OPTION.BYBIT`). On testnet that
is ~716 contracts across ~12 expiries. After ATM selection the strategy calls `subscribe_quote_ticks`,
`subscribe_mark_prices` and `subscribe_option_greeks` on the two legs (all via the option ticker channel).

**5. ATM strike** (`straddle/atm.py`). Reference = BTC **index price** from the BTCUSDT perp ticker
(fallback: perp mid). Chain is grouped `expiry → strike → {CALL, PUT}`; expiries within 6h are skipped;
only strikes where both a Call and a Put exist count; ATM = `argmin |strike − reference|`.

**6. How the strategy receives events.** Nautilus calls `on_index_price`, `on_quote_tick`,
`on_mark_price`, `on_option_greeks` for data, and `on_order_*` / `on_position_*` for execution events.
A 15s timer prints a status table.

**7. Order submission.** `order_factory.limit(... SELL, qty, price, GTC)` → `submit_order()` →
RiskEngine checks (precision, quantity, etc.) → ExecutionEngine routes to the Bybit exec client →
REST/WS-trade `order/create` with `category=option`, `orderLinkId = client_order_id`.

**8. Acknowledgement / execution.** Bybit pushes updates on the private `order` and `execution`
WebSocket topics; the exec client turns them into `OrderAccepted`, `OrderRejected`, `OrderFilled`
events (matched via `client_order_id`/`venue_order_id`). These update the order in the Cache and
are delivered to `on_order_accepted`, `on_order_filled`, etc.

**9. Position tracking.** Each `OrderFilled` is applied by the ExecutionEngine to a `Position`
(one per instrument, NETTING on Bybit) → `PositionOpened/Changed/Closed` events. The status
timer reads `cache.positions_open(strategy_id=...)` and computes unrealized PnL at the ask
(cost to buy back), or mark if no ask.

**10. One leg fills, the other doesn't.** Level 1 does nothing automatically: the filled leg stays a
naked short and the other order keeps resting (visible in the status table as `ACCEPTED`/
`PARTIALLY_FILLED`). Handling this (cancel/chase/flatten) is part of Level 2.
