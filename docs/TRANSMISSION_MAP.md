# Transmission map reference

_Generated 2026-10-21 08:51 UTC by `tools/generate_transmission_docs.py`. Do not edit by hand._

**Note:** The JSON map contains **75 event types**. This table shows the **73** that participate in detection via `EVENT_SIGNALS`. The two excluded are `GENERAL_MACRO` (used only as a category fallback for unclassified headlines) and `GOLD_ETF_FLOW` (removed in Step 4c in favor of its `_IN`/`_OUT` variants, which carry their own sign).

## How to read this

Each event type has a **detection rule** (keywords that fire it) and a per-asset mapping of `[sign, magnitude]`:

- **sign** — `1` bullish, `-1` bearish, `0` no side. A `0` contributes to the `directionless` total, which lowers certainty in `_confidence_from` without adding to either bull or bear.
- **magnitude** — per-asset sensitivity in `0.0–1.5`. It scales the cue magnitude inside `resolve()`, and it is also the `asset_sensitivity` used by `_signal_weight`.

`resolve()` combines every matching event as `sign × magnitude × cue_scale × specificity × driver_rank`, where `specificity = 1 / (1 + 0.5 × index)` — so an event listed **earlier** in `EVENT_SIGNALS` counts for more. One headline can fire several events; they are summed.

An **absent** asset key is NOT the same as `[0, x]`: absent coerces to `NEUTRAL (0, 0.0)` and contributes nothing at all, where `[0, x]` still registers as real evidence and dents confidence. Step 4e filled the five absent `EQUITY` keys for exactly this reason.

## Event types

| # | Event type | Detection rule | XAUUSD | XAGUSD | BTCUSD | EQUITY |
|---|---|---|---|---|---|
| 1 | `HAWKISH_POLICY` | any of: `hawkish`, `rate hike`, `raises rates`, `raise rates`, `higher for longer`, `tightening`<br>…+3 more | ▼ bearish `1` | ▼ bearish `0.92` | ▼ bearish `0.88` | ▼ bearish `0.85` |
| 2 | `RECESSION_CUT` | any of: `rate cut`, `cut rates`, `cuts rates`, `dovish`, `easing`, `stimulus`<br>**and** one of: `recession`, `slump`, `slowdown`, `contraction`, `downturn` | ▲ bullish `1.1` | ○ no side `0.45` | ▼ bearish `0.55` | ▲ bullish `0.7` |
| 3 | `DOVISH_POLICY` | any of: `dovish`, `rate cut`, `cuts rates`, `cut rates`, `pivot`, `easing`<br>…+1 more | ▲ bullish `1` | ▲ bullish `0.9` | ▲ bullish `0.82` | ▲ bullish `0.85` |
| 4 | `REAL_YIELDS_RISING` | any of: `real yields rise`, `real yields rising`, `real yields climbed`, `real yields higher`, `real yields spike`, `real yields spiked`<br>…+9 more<br>**and** one of: `real`, `yield`, `tips`, `inflation protected` | ▼ bearish `1.15` | ▼ bearish `1` | ▼ bearish `0.85` | ▼ bearish `0.8` |
| 5 | `REAL_YIELDS_FALLING` | any of: `real yields fall`, `real yields falling`, `real yields dropped`, `real yields lower`, `real yields drop`, `real yields decline`<br>…+9 more<br>**and** one of: `real`, `yield`, `tips`, `inflation protected` | ▲ bullish `1.1` | ▲ bullish `0.95` | ▲ bullish `0.8` | ▲ bullish `0.75` |
| 6 | `TIPS_AUCTION` | any of: `tips auction`, `tips sale`, `treasury inflation protected`, `real yield auction`, `tips issuance`<br>**and** one of: `tips`, `treasury`, `inflation protected` | ▼ bearish `0.6` | ▼ bearish `0.5` | ▼ bearish `0.4` | ▼ bearish `0.35` |
| 7 | `BREAKEVENS_RISING` | any of: `breakevens rise`, `breakevens rising`, `breakeven inflation rise`, `inflation breakevens climb`, `breakevens widen`, `breakeven spread widens`<br>**and** one of: `breakeven`, `inflation expectation` | ▲ bullish `0.7` | ▲ bullish `0.65` | ○ no side `0.1` | ○ no side `0.15` |
| 8 | `BREAKEVENS_FALLING` | any of: `breakevens fall`, `breakevens falling`, `breakeven inflation fall`, `inflation breakevens drop`, `breakevens narrow`, `breakeven spread narrows`<br>**and** one of: `breakeven`, `inflation expectation` | ▼ bearish `0.7` | ▼ bearish `0.65` | ○ no side `0.1` | ○ no side `0.15` |
| 9 | `RISING_YIELDS` | any of: `yields rise`, `yields rose`, `yields climbed`, `yields climb`, `yields higher`, `yields surged`<br>…+2 more | ▼ bearish `1.1` | ▼ bearish `0.95` | ▼ bearish `0.9` | ▼ bearish `0.85` |
| 10 | `FALLING_YIELDS` | any of: `yields fall`, `yields fell`, `yields eased`, `yields decline`, `lower yields`, `falling yields` | ▲ bullish `0.95` | ▲ bullish `0.85` | ▲ bullish `0.8` | ▲ bullish `0.8` |
| 11 | `STRONG_USD` | any of: `dollar surges`, `dollar rallies`, `dollar index rose`, `dollar strengthens`, `dollar climbed`, `stronger dollar`<br>…+2 more | ▼ bearish `0.95` | ▼ bearish `0.9` | ▼ bearish `0.8` | ▼ bearish `0.55` |
| 12 | `WEAK_USD` | any of: `dollar weakens`, `dollar falls`, `dollar slides`, `weaker dollar`, `dollar declines`, `dxy falls` | ▲ bullish `0.85` | ▲ bullish `0.8` | ▲ bullish `0.7` | ▲ bullish `0.5` |
| 13 | `HOT_INFLATION` | any of: `hotter than expected`, `hot inflation`, `inflation accelerates`, `cpi accelerates` | ▼ bearish `1` | ▼ bearish `0.92` | ▼ bearish `0.9` | ▼ bearish `0.7` |
| 14 | `COOL_INFLATION` | any of: `inflation cools`, `cooling inflation`, `disinflation`, `cpi eases`, `inflation slows` | ▲ bullish `0.92` | ▲ bullish `0.85` | ▲ bullish `0.82` | ▲ bullish `0.65` |
| 15 | `STRONG_JOBS` | any of: `strong payrolls`, `payrolls beat`, `jobs beat`, `strong employment`, `record jobs`, `hiring surge` | ▼ bearish `0.9` | ▼ bearish `0.85` | ▼ bearish `0.88` | ▲ bullish `0.85` |
| 16 | `WEAK_JOBS` | any of: `weak payrolls`, `payrolls miss`, `jobs report misses`, `unemployment rises`, `jobless claims surge`, `layoffs announced` | ▲ bullish `0.85` | ▲ bullish `0.8` | ▲ bullish `0.82` | ▼ bearish `0.8` |
| 17 | `GEOPOLITICAL_DEESCALATION` | any of: `ceasefire`, `cease-fire`, `peace talks`, `peace deal`, `peace agreement`, `de-escalation`<br>…+5 more | ▼ bearish `0.8` | ▼ bearish `0.55` | ▲ bullish `0.25` | ▲ bullish `0.3` |
| 18 | `GEOPOLITICAL_RISK` | any of: `missile`, `invasion`, `war`, `conflict`, `escalation`, `border clash`<br>…+3 more | ▲ bullish `1.2` | ▲ bullish `0.8` | ▼ bearish `0.35` | ▼ bearish `0.3` |
| 19 | `BANKING_STRESS` | any of: `banking crisis`, `bank fails`, `bank collapse`, `default`, `insolvency`, `credit crunch`<br>…+4 more | ▲ bullish `1.15` | ▲ bullish `0.6` | ○ no side `0.9` | ▼ bearish `0.75` |
| 20 | `CRYPTO_HACK` | any of: `exchange hacked`, `exchange hack`, `crypto hack`, `hacked`, `exploit`, `exploited`<br>…+3 more<br>**and** one of: `crypto`, `bitcoin`, `btc`, `exchange`, `defi`, `wallet`…+2 more | ○ no side `0.05` | ○ no side `0.05` | ▼ bearish `1` | ○ no side `0.05` |
| 21 | `CRYPTO_LIQUIDATION` | any of: `liquidations`, `liquidated`, `cascade`, `forced selling` | ○ no side `0.1` | ○ no side `0.1` | ▼ bearish `1.05` | ○ no side `0.05` |
| 22 | `BTC_FUNDING_RATE_SPIKE` | any of: `funding rate spike`, `funding rate surges`, `funding rate spikes`, `negative funding`, `funding turned negative`<br>**and** one of: `btc`, `bitcoin`, `perp`, `perpetual` | ○ no side `0.05` | ○ no side `0.05` | ▼ bearish `1` | ○ no side `0.05` |
| 23 | `BTC_FUNDING_RATE_RESET` | any of: `funding rate reset`, `funding rate normalizes`, `funding rate normalised`, `funding back to neutral`<br>**and** one of: `btc`, `bitcoin`, `perp`, `perpetual` | ○ no side `0.05` | ○ no side `0.05` | ▲ bullish `0.8` | ○ no side `0.05` |
| 24 | `BTC_OPEN_INTEREST_SURGE` | any of: `open interest surge`, `open interest spikes`, `oi surge`, `oi spike`, `open interest jumps`<br>**and** one of: `btc`, `bitcoin`, `perp`, `perpetual` | ○ no side `0.05` | ○ no side `0.05` | ▲ bullish `0.7` | ○ no side `0.05` |
| 25 | `BTC_OPEN_INTEREST_DROP` | any of: `open interest drop`, `open interest falls`, `oi drop`, `oi falls`, `open interest decline`<br>**and** one of: `btc`, `bitcoin`, `perp`, `perpetual` | ○ no side `0.05` | ○ no side `0.05` | ▼ bearish `0.7` | ○ no side `0.05` |
| 26 | `BTC_LONG_LIQUIDATION` | any of: `long liquidation`, `longs liquidated`, `long liquidations`, `longs wiped`, `long positions liquidated`<br>**and** one of: `btc`, `bitcoin`, `crypto` | ○ no side `0.05` | ○ no side `0.05` | ▼ bearish `1.1` | ○ no side `0.05` |
| 27 | `BTC_SHORT_SQUEEZE` | any of: `short squeeze`, `shorts squeezed`, `short covering`, `shorts cover`<br>**and** one of: `btc`, `bitcoin`, `crypto` | ○ no side `0.05` | ○ no side `0.05` | ▲ bullish `1` | ○ no side `0.05` |
| 28 | `BTC_WHALE_ALERT` | any of: `whale alert`, `whale transfer`, `large transfer`, `whale moved`, `whale transaction`<br>**and** one of: `btc`, `bitcoin` | ○ no side `0.05` | ○ no side `0.05` | ○ no side `0.6` | ○ no side `0.05` |
| 29 | `BTC_EXCHANGE_INFLOW` | any of: `exchange inflow`, `exchange inflows`, `binance inflow`, `coinbase inflow`, `deposits surge`, `exchange deposit`<br>**and** one of: `btc`, `bitcoin` | ○ no side `0.05` | ○ no side `0.05` | ▼ bearish `0.85` | ○ no side `0.05` |
| 30 | `BTC_EXCHANGE_OUTFLOW` | any of: `exchange outflow`, `exchange outflows`, `binance outflow`, `coinbase outflow`, `withdrawals surge`, `exchange withdrawal`<br>**and** one of: `btc`, `bitcoin` | ○ no side `0.05` | ○ no side `0.05` | ▲ bullish `0.85` | ○ no side `0.05` |
| 31 | `BTC_STABLECOIN_MINT` | any of: `usdt mint`, `usdc mint`, `tether mint`, `stablecoin mint`, `usdt printed`, `usdc printed`<br>**and** one of: `btc`, `bitcoin`, `crypto` | ○ no side `0.05` | ○ no side `0.05` | ▲ bullish `0.75` | ○ no side `0.05` |
| 32 | `BTC_STABLECOIN_REDEEM` | any of: `usdt redemption`, `usdc redemption`, `tether redemption`, `stablecoin redemption`, `usdt burned`, `usdc burned`<br>**and** one of: `btc`, `bitcoin`, `crypto` | ○ no side `0.05` | ○ no side `0.05` | ▼ bearish `0.75` | ○ no side `0.05` |
| 33 | `BTC_ETF_FLOW` | any of: `bitcoin etf`, `spot etf`, `ibit`, `fbtc`, `btco`, `bitcoin fund`<br>…+2 more<br>**and** one of: `bitcoin`, `btc`, `crypto` | ○ no side `0.05` | ○ no side `0.1` | ▲ bullish `1.1` | ○ no side `0.15` |
| 34 | `GOLD_FUNDING_RATE_SPIKE` | any of: `gold funding rate spike`, `gold funding rate surges`, `gold funding rate spikes`, `negative gold funding`, `gold funding turned negative`<br>**and** one of: `gold`, `xau`, `comex`, `futures` | ▼ bearish `0.8` | ▼ bearish `0.6` | ○ no side `0.05` | ○ no side `0.05` |
| 35 | `GOLD_FUNDING_RATE_RESET` | any of: `gold funding rate reset`, `gold funding rate normalizes`, `gold funding rate normalised`, `gold funding back to neutral`<br>**and** one of: `gold`, `xau`, `comex`, `futures` | ▲ bullish `0.6` | ▲ bullish `0.45` | ○ no side `0.05` | ○ no side `0.05` |
| 36 | `GOLD_OPEN_INTEREST_SURGE` | any of: `gold open interest surge`, `gold open interest spikes`, `gold oi surge`, `gold oi spike`, `gold open interest jumps`<br>**and** one of: `gold`, `xau`, `comex`, `futures` | ▲ bullish `0.7` | ▲ bullish `0.55` | ○ no side `0.05` | ○ no side `0.05` |
| 37 | `GOLD_OPEN_INTEREST_DROP` | any of: `gold open interest drop`, `gold open interest falls`, `gold oi drop`, `gold oi falls`, `gold open interest decline`<br>**and** one of: `gold`, `xau`, `comex`, `futures` | ▼ bearish `0.7` | ▼ bearish `0.55` | ○ no side `0.05` | ○ no side `0.05` |
| 38 | `GOLD_LONG_LIQUIDATION` | any of: `gold long liquidation`, `gold longs liquidated`, `gold long liquidations`, `gold longs wiped`, `gold long positions liquidated`<br>**and** one of: `gold`, `xau`, `comex`, `futures` | ▼ bearish `0.9` | ▼ bearish `0.7` | ○ no side `0.05` | ○ no side `0.05` |
| 39 | `GOLD_SHORT_SQUEEZE` | any of: `gold short squeeze`, `gold shorts squeezed`, `gold short covering`, `gold shorts cover`<br>**and** one of: `gold`, `xau`, `comex`, `futures` | ▲ bullish `0.85` | ▲ bullish `0.65` | ○ no side `0.05` | ○ no side `0.05` |
| 40 | `GOLD_EXCHANGE_INFLOW` | any of: `gold exchange inflow`, `gold exchange inflows`, `comex inflow`, `gold inflow`, `gold deposits surge`<br>**and** one of: `gold`, `xau`, `comex`, `etf`, `vault` | ▼ bearish `0.75` | ▼ bearish `0.55` | ○ no side `0.05` | ○ no side `0.05` |
| 41 | `GOLD_EXCHANGE_OUTFLOW` | any of: `gold exchange outflow`, `gold exchange outflows`, `comex outflow`, `gold outflow`, `gold withdrawals surge`<br>**and** one of: `gold`, `xau`, `comex`, `etf`, `vault` | ▲ bullish `0.75` | ▲ bullish `0.55` | ○ no side `0.05` | ○ no side `0.05` |
| 42 | `GOLD_ETF_FLOW_IN` | any of: `gold etf inflow`, `gold etf inflows`, `gld inflow`, `gld inflows`, `gold fund inflow`, `gold fund inflows`<br>…+2 more<br>**and** one of: `gold`, `xau`, `gld` | ▲ bullish `1.1` | ▲ bullish `0.3` | ○ no side `0.05` | ○ no side `0.1` |
| 43 | `GOLD_ETF_FLOW_OUT` | any of: `gold etf outflow`, `gold etf outflows`, `gld outflow`, `gld outflows`, `gold fund outflow`, `gold fund outflows`<br>…+4 more<br>**and** one of: `gold`, `xau`, `gld` | ▼ bearish `1.1` | ▼ bearish `0.3` | ○ no side `0.05` | ○ no side `0.1` |
| 44 | `CENTRAL_BANK_GOLD_BUYING` | any of: `central bank buying`, `central bank buys`, `central bank purchase`, `reserve accumulation`, `gold reserves rise`, `official sector buying`<br>…+2 more<br>**and** one of: `gold`, `xau`, `reserves`, `central bank` | ▲ bullish `1.2` | ▲ bullish `0.6` | ○ no side `0.05` | ○ no side `0.05` |
| 45 | `CENTRAL_BANK_GOLD_SELLING` | any of: `central bank selling`, `central bank sells`, `central bank sale`, `reserve reduction`, `gold reserves fall`, `official sector selling`<br>…+2 more<br>**and** one of: `gold`, `xau`, `reserves`, `central bank` | ▼ bearish `1.2` | ▼ bearish `0.6` | ○ no side `0.05` | ○ no side `0.05` |
| 46 | `RISK_ON` | any of: `risk-on`, `risk on`, `risk appetite improves`, `risk appetite returns`, `risk appetite rebuilds`, `investors embrace risk`<br>…+2 more | ▼ bearish `0.45` | ○ no side `0.55` | ▲ bullish `1` | ▲ bullish `0.9` |
| 47 | `RISK_OFF` | any of: `risk-off`, `risk aversion`, `flight to quality`, `panic selling`, `safe haven demand`, `safe-haven demand` | ▲ bullish `0.65` | ▼ bearish `0.35` | ▼ bearish `1.05` | ▼ bearish `0.9` |
| 48 | `CHINA_DEMAND` | any of: `china`, `chinese`, `beijing`, `yuan`, `caixin`, `shanghai`<br>…+2 more<br>**and** one of: `pmi`, `factory`, `manufacturing`, `industrial`, `output`, `exports`…+12 more | ▲ bullish `0.45` | ▲ bullish `1` | ○ no side `0.25` | ○ no side `0.1` |
| 49 | `SILVER_CHINA_DEMAND` | any of: `china pmi`, `china manufacturing pmi`, `pmi reading`, `factory output`, `factory gate`, `industrial output`<br>…+45 more | ○ no side `0.2` | ▲ bullish `1` | ○ no side `0.05` | ○ no side `0.05` |
| 50 | `INDUSTRIAL_DEMAND` | any of: `pmi`, `manufacturing`, `factory output`, `industrial production`, `solar`, `electronics`<br>…+1 more | ○ no side `0.25` | ▲ bullish `1` | ○ no side `0.2` | ○ no side `0.05` |
| 51 | `SILVER_INDUSTRIAL_DEMAND` | any of: `solar`, `photovoltaic`, `pv `, `solar panel`, `solar cell`, `solar farm`<br>…+47 more | ○ no side `0.1` | ▲ bullish `1` | ○ no side `0.05` | ○ no side `0.05` |
| 52 | `GOLD_SILVER_RATIO_HIGH` | any of: `gold silver ratio`, `gold-to-silver ratio`, `gold/silver ratio`, `gold silver ratio high`, `ratio elevated`, `ratio at historic highs`<br>…+28 more | ▼ bearish `0.4` | ▲ bullish `1` | ○ no side `0.05` | ○ no side `0.05` |
| 53 | `GOLD_SILVER_RATIO_LOW` | any of: `gold silver ratio low`, `gold-to-silver ratio low`, `ratio compressed`, `ratio compression`, `ratio tight`, `ratio at historic lows`<br>…+16 more | ▲ bullish `0.4` | ▼ bearish `1` | ○ no side `0.05` | ○ no side `0.05` |
| 54 | `COMMODITY_SUPPLY` | any of: `mine`, `mining`, `refinery`, `gold supply`, `silver supply`, `opec cuts` | ▲ bullish `0.6` | ▲ bullish `0.75` | ○ no side `0.1` | ○ no side `0.05` |
| 55 | `OIL_SUPPLY_TIGHTNESS` | any of: `opec cuts`, `opec+ cuts`, `cuts output`, `output cut`, `production cut`, `supply cut`<br>…+32 more | ○ no side `0.2` | ○ no side `0.15` | ○ no side `0.1` | ▼ bearish `0.3` |
| 56 | `OIL_SUPPLY_GLUT` | any of: `opec raises output`, `opec increases output`, `opec boost`, `opec+ raises`, `output increase`, `supply surge`<br>…+36 more | ○ no side `0.1` | ○ no side `0.1` | ○ no side `0.05` | ▲ bullish `0.2` |
| 57 | `EARNINGS_BEAT` | any of: `earnings beat`, `profit beats`, `revenue beat`, `sales beat`, `beats estimates`, `beat estimate`<br>…+17 more<br>**and** one of: `earnings`, `profit`, `revenue`, `eps`, `quarter`, `guidance`…+1 more | ○ no side `0.05` | ○ no side `0.05` | ○ no side `0.1` | ▲ bullish `1` |
| 58 | `EARNINGS_MISS` | any of: `earnings miss`, `profit miss`, `revenue miss`, `sales miss`, `misses estimates`, `miss estimate`<br>…+20 more<br>**and** one of: `earnings`, `profit`, `revenue`, `eps`, `quarter`, `guidance`…+1 more | ○ no side `0.05` | ○ no side `0.05` | ○ no side `0.1` | ▼ bearish `1` |
| 59 | `GUIDANCE_RAISED` | any of: `raises guidance`, `lifts guidance`, `guidance raised`, `guidance increase`, `outlook raised`, `outlook improved`<br>…+3 more | ○ no side `0.05` | ○ no side `0.05` | ○ no side `0.1` | ▲ bullish `0.95` |
| 60 | `GUIDANCE_CUT` | any of: `cuts guidance`, `lowers guidance`, `guidance cut`, `guidance lowered`, `outlook cut`, `outlook lowered`<br>…+5 more | ○ no side `0.05` | ○ no side `0.05` | ○ no side `0.1` | ▼ bearish `0.95` |
| 61 | `INDEX_FLOW` | any of: `index inflow`, `index inflows`, `index fund`, `index fund inflow`, `index rebalance`, `index weight`<br>…+9 more<br>**and** one of: `s&p`, `index`, `equity`, `equities`, `nasdaq`, `nyse`…+5 more | ○ no side `0.1` | ○ no side `0.1` | ○ no side `0.15` | ▲ bullish `0.85` |
| 62 | `SECTOR_ROTATION` | any of: `sector rotation`, `rotation into`, `rotation out of`, `sector shift`, `risk rotation`, `rotation trade` | ○ no side `0.05` | ○ no side `0.05` | ○ no side `0.05` | ○ no side `0.3` |
| 63 | `RATE_REPRICING` | any of: `markets price`, `markets are pricing`, `priced in a cut`, `priced in a hike`, `odds of a hike`, `odds of a cut`<br>…+7 more | ▼ bearish `0.9` | ▼ bearish `0.85` | ▼ bearish `0.8` | ▼ bearish `0.8` |
| 64 | `STOCK_RALLY` | any of: `stocks rally`, `stock rally`, `equities surge`, `equity surge`, `s&p 500 rises`, `s&p rises`<br>…+8 more | ▼ bearish `0.3` | ○ no side `0.2` | ▲ bullish `0.55` | ▲ bullish `1` |
| 65 | `STOCK_PLUNGE` | any of: `stocks plunge`, `stock plunge`, `equities plunge`, `equity plunge`, `s&p 500 falls`, `s&p falls`<br>…+9 more | ▲ bullish `0.35` | ○ no side `0.2` | ▼ bearish `0.55` | ▼ bearish `1` |
| 66 | `VIX_SPIKE` | any of: `vix spikes`, `vix spike`, `vix surges`, `vix surge`, `vix rises`, `vix rose`<br>…+6 more | ▲ bullish `0.4` | ○ no side `0.2` | ▼ bearish `0.5` | ▼ bearish `0.9` |
| 67 | `VIX_COMPRESSION` | any of: `vix falls`, `vix fell`, `vix eases`, `vix declines`, `vix lower`, `vix calm`<br>…+3 more | ▼ bearish `0.25` | ○ no side `0.15` | ▲ bullish `0.4` | ▲ bullish `0.7` |
| 68 | `CORP_EARNINGS_SEASON` | any of: `earnings season`, `reporting season`, `corporate earnings`, `c-suite guidance`, `management guidance` | ○ no side `0.05` | ○ no side `0.05` | ○ no side `0.05` | ○ no side `0.25` |
| 69 | `REGULATION_CLAMP` | any of: `ban`, `bans`, `ban on`, `banned`, `enforce`, `enforcement`<br>…+26 more | ○ no side `0.05` | ○ no side `0.05` | ▼ bearish `1.05` | ○ no side `0.05` |
| 70 | `REGULATION_CLARITY` | any of: `approved`, `approval`, `approves`, `framework`, `frameworks`, `legal clarity`<br>…+24 more<br>**and** one of: `bitcoin`, `btc`, `crypto`, `etf`, `sec`, `cftc`…+6 more | ○ no side `0.05` | ○ no side `0.05` | ▲ bullish `1` | ○ no side `0.05` |
| 71 | `CRYPTO_REGULATION` | any of: `crypto regulation`, `regulates crypto`, `crypto bill`, `stablecoin bill`, `stablecoin legislation`, `custody rule`<br>…+7 more<br>**and** one of: `crypto`, `bitcoin`, `btc`, `token`, `stablecoin`, `digital asset` | ○ no side `0.05` | ○ no side `0.05` | ▼ bearish `0.8` | ○ no side `0.05` |
| 72 | `LIQUIDITY_EASING` | any of: `quantitative easing`, `quantitative easing programme`, `quantitative easing program`, `qe`, `asset purchase`, `asset purchases`<br>…+24 more | ○ no side `0.3` | ○ no side `0.25` | ▲ bullish `0.95` | ○ no side `0.5` |
| 73 | `LIQUIDITY_TIGHTENING` | any of: `quantitative tightening`, `qt`, `balance sheet runoff`, `balance sheet shrink`, `balance sheet shrinking`, `balance sheet reduction`<br>…+26 more | ○ no side `0.3` | ○ no side `0.25` | ▼ bearish `0.95` | ○ no side `0.5` |

## Driver ranks

`DRIVER_RANKINGS` in `app/config.py`, used as the final multiplier in `resolve()`. A rank of `None` means the event is not ranked for that asset and `0.3` is used instead.

| Event type | XAUUSD | XAGUSD | BTCUSD | EQUITY |
|---|---|---|---|---|
| `HAWKISH_POLICY` | — | — | — | — |
| `RECESSION_CUT` | — | — | — | — |
| `DOVISH_POLICY` | — | — | — | — |
| `REAL_YIELDS_RISING` | — | — | — | — |
| `REAL_YIELDS_FALLING` | — | — | — | — |
| `TIPS_AUCTION` | — | — | — | — |
| `BREAKEVENS_RISING` | — | — | — | — |
| `BREAKEVENS_FALLING` | — | — | — | — |
| `RISING_YIELDS` | — | — | — | — |
| `FALLING_YIELDS` | — | — | — | — |
| `STRONG_USD` | — | — | — | — |
| `WEAK_USD` | — | — | — | — |
| `HOT_INFLATION` | — | — | — | — |
| `COOL_INFLATION` | — | — | — | — |
| `STRONG_JOBS` | — | — | — | — |
| `WEAK_JOBS` | — | — | — | — |
| `GEOPOLITICAL_DEESCALATION` | — | — | — | — |
| `GEOPOLITICAL_RISK` | — | — | — | — |
| `BANKING_STRESS` | — | — | — | — |
| `CRYPTO_HACK` | — | — | — | — |
| `CRYPTO_LIQUIDATION` | — | — | — | — |
| `BTC_FUNDING_RATE_SPIKE` | — | — | — | — |
| `BTC_FUNDING_RATE_RESET` | — | — | — | — |
| `BTC_OPEN_INTEREST_SURGE` | — | — | — | — |
| `BTC_OPEN_INTEREST_DROP` | — | — | — | — |
| `BTC_LONG_LIQUIDATION` | — | — | — | — |
| `BTC_SHORT_SQUEEZE` | — | — | — | — |
| `BTC_WHALE_ALERT` | — | — | — | — |
| `BTC_EXCHANGE_INFLOW` | — | — | — | — |
| `BTC_EXCHANGE_OUTFLOW` | — | — | — | — |
| `BTC_STABLECOIN_MINT` | — | — | — | — |
| `BTC_STABLECOIN_REDEEM` | — | — | — | — |
| `BTC_ETF_FLOW` | — | — | — | — |
| `GOLD_FUNDING_RATE_SPIKE` | — | — | — | — |
| `GOLD_FUNDING_RATE_RESET` | — | — | — | — |
| `GOLD_OPEN_INTEREST_SURGE` | — | — | — | — |
| `GOLD_OPEN_INTEREST_DROP` | — | — | — | — |
| `GOLD_LONG_LIQUIDATION` | — | — | — | — |
| `GOLD_SHORT_SQUEEZE` | — | — | — | — |
| `GOLD_EXCHANGE_INFLOW` | — | — | — | — |
| `GOLD_EXCHANGE_OUTFLOW` | — | — | — | — |
| `GOLD_ETF_FLOW_IN` | — | — | — | — |
| `GOLD_ETF_FLOW_OUT` | — | — | — | — |
| `CENTRAL_BANK_GOLD_BUYING` | — | — | — | — |
| `CENTRAL_BANK_GOLD_SELLING` | — | — | — | — |
| `RISK_ON` | — | — | — | — |
| `RISK_OFF` | — | — | — | — |
| `CHINA_DEMAND` | — | — | — | — |
| `SILVER_CHINA_DEMAND` | — | — | — | — |
| `INDUSTRIAL_DEMAND` | — | — | — | — |
| `SILVER_INDUSTRIAL_DEMAND` | — | — | — | — |
| `GOLD_SILVER_RATIO_HIGH` | — | — | — | — |
| `GOLD_SILVER_RATIO_LOW` | — | — | — | — |
| `COMMODITY_SUPPLY` | — | — | — | — |
| `OIL_SUPPLY_TIGHTNESS` | — | — | — | — |
| `OIL_SUPPLY_GLUT` | — | — | — | — |
| `EARNINGS_BEAT` | — | — | — | — |
| `EARNINGS_MISS` | — | — | — | — |
| `GUIDANCE_RAISED` | — | — | — | — |
| `GUIDANCE_CUT` | — | — | — | — |
| `INDEX_FLOW` | — | — | — | — |
| `SECTOR_ROTATION` | — | — | — | — |
| `RATE_REPRICING` | — | — | — | — |
| `STOCK_RALLY` | — | — | — | — |
| `STOCK_PLUNGE` | — | — | — | — |
| `VIX_SPIKE` | — | — | — | — |
| `VIX_COMPRESSION` | — | — | — | — |
| `CORP_EARNINGS_SEASON` | — | — | — | — |
| `REGULATION_CLAMP` | — | — | — | — |
| `REGULATION_CLARITY` | — | — | — | — |
| `CRYPTO_REGULATION` | — | — | — | — |
| `LIQUIDITY_EASING` | — | — | — | — |
| `LIQUIDITY_TIGHTENING` | — | — | — | — |

## Deliberate design decisions

- **`GEOPOLITICAL_RISK` is bullish for gold (+1.20), bearish for bitcoin (-0.35) and bearish for equity (-0.30).** The per-asset split is intentional: a conflict is a safe-haven bid for metals and a risk-off shock for the other two.
- **`CRYPTO_REGULATION` is signed bearish for BTCUSD** `[-1, 0.80]` (Step 4a). It used to be `[0, 1.00]` — directionless *and* the largest magnitude in the map, which is the worst combination: every crypto-regulation headline reached maximum weight with no side. It is also ordered **after** `REGULATION_CLAMP` and `REGULATION_CLARITY` so a signed rule wins whenever one applies.
- **`GOLD_ETF_FLOW` is never detected** (Step 4c). It duplicated `GOLD_ETF_FLOW_IN` / `GOLD_ETF_FLOW_OUT`, so one gold-ETF headline fired two events and was summed twice. Only the `_IN` / `_OUT` variants, which carry the sign themselves, remain.
- **`INDEX_FLOW` requires an equity-index qualifier** (Step 4c). The bare `etf inflow` phrases made every gold- and bitcoin-ETF headline also count as an equity index flow.
- **`RISK_ON` no longer fires on `stocks rally`** (Step 4d). That phrase belongs to `STOCK_RALLY`, which is the equity-specific rule; having it in both double-counted every equity rally.
- **`RATE_REPRICING` carries only repricing language** (Step 4d). It shared nine phrases with `HAWKISH_POLICY`, so every hawkish headline double-counted.

## Known model choices (not defects)

These look wrong against conventional market expectations but are deliberate and documented:

- `STRONG_JOBS` is bullish for equity (+0.85) and bearish for the metals (-0.90) — a strong print tightens the rate path.
- `RECESSION_CUT` is bearish for bitcoin and no-side for silver: a recession-driven cut does not transmit to silver through that channel.
- `SECTOR_ROTATION` and `CORP_EARNINGS_SEASON` are directionless by construction — they describe money moving, not a direction.

