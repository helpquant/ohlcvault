# OHLCVault

> OHLCV = Open/High/Low/Close/Volume, the universal bar format.
> Vault = immutable, checksummed snapshots.

Reproducible daily OHLCV data for **A-share, Hong Kong and US markets** — served as
static files from a CDN. No API keys, no rate limits, no per-request billing.

```python
import ohlcvault as ov

ov.connect()
df = ov.daily("600519.SH").to_pandas()      # 跨月自动拼接
ov.cross_section("cn", 20260918, limit=50)  # 当日全市场截面，按成交额排序
```

## Why this exists

Most free market-data endpoints are **APIs**: stateful, rate-limited, silently
revised, and impossible to reproduce. A backtest that ran last month can't be rerun
today with the same inputs.

OHLCVault publishes **immutable monthly shards with checksums** instead. Each read is
anchored to a snapshot id you can write down and reproduce later, on any machine.

## Install

```bash
pip install ohlcvault            # core, zero runtime dependencies
pip install "ohlcvault[pandas]"  # + DataFrame helpers
```

The core has **no third-party dependencies** — only the standard library. That's
deliberate: a data client shouldn't drag a dependency tree into your project, and it
matters even more for agent/tooling contexts.

## Usage

### Everything (the five things you actually need)

```python
import ohlcvault as ov
ov.connect()

# 1. Trading calendar
ov.calendar("cn", start=20260101)

# 2. Stock daily bars (cross-month stitching is handled for you)
ov.daily("600519.SH", start=20260101, end=20260918)

# 3. Index daily bars — a separate namespace, never mixed with stocks
ov.index_daily("000300.SH")

# 4. Symbol list — includes delisted stocks
ov.symbols("cn", type="stock")
ov.symbols("cn", type="stock", status="delisted")

# 5. Daily cross-section — sorted by turnover, no extra data files
ov.cross_section("cn", 20260918, sort_by="amount", limit=50)
```

### Batch backtests

Month shards hold **every symbol in the market** for that month. Loading 200 symbols
one-by-one would decompress the same file 200 times:

```python
bars = ov.daily_many(["600519.SH", "000001.SZ", "300750.SZ"], start=20260101)
bars["600519.SH"].to_pandas()
```

### Adjustment is a view, not a stored field

The dataset stores **unadjusted prices only**, plus the official cumulative
back-adjustment factor. Forward/backward adjusted prices are computed client-side:

```python
b = ov.daily("600519.SH")
ov.adjust(b, to="hfq")   # 后复权
ov.adjust(b, to="qfq")   # 前复权
```

This is not a limitation — it's the reason historical files never change. If
forward-adjusted prices were stored, every dividend would rewrite all of history, and
`immutable` caching would be impossible.

### Reproducibility

```python
st = ov.connect()
sid = st.snapshot                       # e.g. "6b197df3723871c5"
ov.connect(snapshot=sid)                # later, anywhere: exact same inputs
```

### Offline / self-hosted mirrors

A mirror can be an HTTP(S) URL **or a local directory**:

```python
ov.connect(mirrors=["/path/to/data"])
```

Mirrors are tried in order; whichever one succeeds is promoted to first place. Every
file is checked against the `sha256` in the snapshot manifest, and anything that
fails is **discarded and the next mirror is tried** — bad bytes are never handed to
the caller.

## Data integrity

| Guarantee | How |
|---|---|
| No silently-corrupted data | Every file verified against the snapshot's `sha256` |
| No silently-changed history | Sealed months are never rewritten |
| No unverifiable numbers | Missing adjustment factors raise, instead of returning raw prices |
| No hidden survivorship bias | Delisted stocks are kept in the universe and in the data |
| Byte-for-byte reproduction | Fixed-point integers, `gzip` with `MTIME=0`, no wall-clock timestamps |

**Delisted stocks matter.** If your backtest universe only contains companies that
are still listed today, your historical returns are systematically overstated.
`ov.symbols("cn", status="delisted")` returns them, and their daily bars are complete
over `ipo … out`.

## Coverage and known gaps

Coverage is declared explicitly in `meta/symbols/{market}.json` under `coverage`, and
`ov.connect()` prints it on startup. Current state:

| Market | Status | Gaps |
|---|---|---|
| `cn` | Daily bars + indices, 2000→present | **No Beijing Stock Exchange** (upstream source doesn't provide it) |
| `hk` | Symbol list only | No daily bars yet; **list is current listings only** |
| `us` | Symbol list only | No daily bars yet; **list is current listings only** |

`hk` / `us` symbol lists carry **survivorship bias** (the upstream source only returns
currently-listed securities) and have no IPO dates. Their daily bars are pending.

An honest data project states its gaps. A dataset that quietly omits them is worse
than one that is merely incomplete.

## API

| Function | Purpose |
|---|---|
| `connect(mirrors=, cache_dir=, snapshot=)` | Build the default client |
| `symbols(market, type=, status=, board=)` | Symbol list |
| `symbol(code)` | Single symbol entry |
| `calendar(market, start=, end=)` | Trading calendar |
| `daily(code, start=, end=)` | Stock daily bars, cross-month stitching |
| `index_daily(code, start=, end=)` | Index daily bars |
| `daily_many(codes, start=, end=)` | Batch read (preferred for backtests) |
| `cross_section(market, date, sort_by=, limit=)` | Daily cross-section |
| `adjust(bars_or_df, to="qfq"\|"hfq"\|"none")` | Adjustment view |
| `snapshot()` | Current snapshot id |

Date parameters (`start` / `end` / `date`) accept an `int` `YYYYMMDD` (preferred)
or common string forms — `"2026-09-18"`, `"20260918"`, `"2026/09/18"`. Anything
unparseable raises `DateError` instead of failing deep inside the library.

The frozen data contract lives in [`SPEC.md`](SPEC.md) — the client and the pipeline
share nothing but this document and the files it describes. `schema/example-*.json`
are machine-generated from real data, so the examples cannot drift from the contract.

## License

MIT. See [LICENSE](LICENSE).

Data is gathered from public sources. Verify before relying on it for anything
consequential.
