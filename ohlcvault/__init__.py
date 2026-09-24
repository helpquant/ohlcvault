"""OHLCVault —— 可复现、带校验和的公开行情数据集客户端。

    OHLCV = Open/High/Low/Close/Volume, the universal bar format.
    Vault = immutable, checksummed snapshots.

设计立场（决定了这个库为什么长这样）：

- **数据是事实，复权是视图。** 盘上只存不复权原始价 + 官方复权因子，
  前/后复权由客户端算（`adjust()`）。所以历史文件永远不会因分红送转而改变，
  `immutable` 缓存才成立，用户才有可能「一生下载一次」。
- **没有可信校验的数据不算数据。** 每个文件都对快照清单里的 sha256 校验，
  不匹配即丢弃并降级到下一个镜像 —— 绝不把脏数据交给调用方。
- **可复现是一等公民。** 每次读取都锚定在一个快照 id 上（`snapshot()`），
  把它记下来就能在任何机器、任何时间重建同一份输入。

快速开始：

    import ohlcvault as ov

    ov.connect()                                  # 默认镜像链
    ov.daily("600519.SH", start=20260701, end=20260918)
    ov.cross_section("cn", 20260918, limit=50)    # 按成交额排序的日截面
    ov.symbols("cn", type="stock", status="delisted")   # 退市股也在池内
    ov.calendar("cn", start=20260101)

离线 / 自建镜像：

    ov.connect(mirrors=["/path/to/data"])         # 本地目录也是合法镜像
    ov.connect(mirrors=[...], snapshot="be592f7a7fb5e4e3")   # 冻结复现
"""
from __future__ import annotations

from pathlib import Path

from .adjust import ADJUSTMENTS, apply_adjust
from .client import MarketClient, sha256_bytes, sha256_file
from .codes import market_of as market_of
from .codes import normalize as normalize_symbol
from .codes import split as split_symbol
from .codes import to_api, to_storage
from .config import DEFAULT_CACHE, DEFAULT_MIRRORS, MARKETS, TIMEOUT, mirrors_from_env
from .errors import (
    ConfigError,
    ContractError,
    DateError,
    HashMismatch,
    NotSupported,
    OhlcvaultError,
    SnapshotUnavailable,
    SymbolError,
    UnknownMarket,
)
from .store import Bars, Store, period_of, periods_between

__version__ = "0.1.0"

__all__ = [
    "Bars", "Store", "MarketClient", "connect", "use", "reset",
    "symbols", "symbol", "calendar", "daily", "index_daily", "daily_many",
    "cross_section", "adjust", "snapshot", "coverage", "coverage_note",
    "period_of", "periods_between", "sha256_bytes", "sha256_file",
    "normalize_symbol", "to_api", "to_storage", "market_of", "split_symbol",
    "__version__",
    # errors
    "OhlcvaultError", "ConfigError", "ContractError", "DateError", "HashMismatch",
    "NotSupported", "SnapshotUnavailable", "SymbolError", "UnknownMarket",
]

_default: Store | None = None


# ── 连接 ────────────────────────────────────────────────────────────────

def connect(
    mirrors: list[str] | None = None,
    cache_dir: str | Path | None = None,
    snapshot: str | None = None,
    timeout: int = TIMEOUT,
    validate: bool = True,
    shard_cache: int = 4,
    quiet: bool = False,
) -> Store:
    """建立（并记住）默认 Store。

    `mirrors` 缺省时依次读环境变量 `OHLCVAULT_MIRRORS` 与内置默认链。
    镜像既可以是 http(s) URL，也可以是**本地目录** —— 后者让离线复现和
    自建镜像站都不需要额外代码。
    """
    global _default
    ms = mirrors or mirrors_from_env()
    if not ms:
        raise ConfigError("没有可用镜像：显式传入 mirrors 或设置 OHLCVAULT_MIRRORS")
    cli = MarketClient(ms, cache_dir or DEFAULT_CACHE, snapshot=snapshot, timeout=timeout)
    st = Store(cli, validate=validate, shard_cache=shard_cache)
    _default = st
    if not quiet:
        _announce(st)
    return st


def _announce(st: Store) -> None:
    """首次连接时把「你拿到的是哪一份数据、它缺什么」说出来。

    静默地返回一份有已知缺口的数据，是这个项目最不想犯的错误。
    """
    sid = st.snapshot
    print(f"[ohlcvault] 快照 {sid}  镜像 {len(st.client.mirrors)} 个")
    for mkt in MARKETS:
        try:
            note = st.coverage_note(mkt)
        except OhlcvaultError:
            continue
        if note:
            print(f"[ohlcvault] {mkt} 覆盖说明：{note}")


def use(store: Store) -> Store:
    """指定默认 Store（例如已 pinned 的实例）。"""
    global _default
    _default = store
    return store


def reset() -> None:
    """清掉默认 Store（测试用）。"""
    global _default
    _default = None


def _store() -> Store:
    global _default
    if _default is None:
        _default = connect()
    return _default


# ── 五项需求的模块级入口（委托给默认 Store）────────────────────────────

def symbols(market: str, type: str | None = None,   # noqa: A002
            status: str | None = None, board: str | None = None,
            include_indices: bool = True) -> list[dict]:
    """标的清单：代码 / 名称 / 上市日 / 退市状态 / 类型 / 板块。

    **含退市股**（`status="delisted"`）。回测池若只含今天还活着的股票，
    历史收益会被系统性高估 —— 这是本数据集相对爬虫类源的结构性优势。
    """
    return _store().symbols(market, type=type, status=status, board=board,
                            include_indices=include_indices)


def symbol(code: str, market: str | None = None) -> dict:
    """查单个标的的清单条目。"""
    return _store().symbol(code, market=market)


def calendar(market: str, start: int | str | None = None,
             end: int | str | None = None) -> list[int]:
    """交易日历（升序 `YYYYMMDD`）。权威区间只到今天，不承诺未来。

    `start` / `end` 接受 `20260918` 或 `"2026-09-18"`。
    """
    return _store().calendar(market, start=start, end=end)


def daily(code: str, start: int | str | None = None, end: int | str | None = None,
          market: str | None = None, refresh: bool = False) -> Bars:
    """股票日线（跨月自动拼接）。

    `start` / `end` 接受 `20260918` 或 `"2026-09-18"`；非法形态抛 `DateError`。
    """
    return _store().daily(code, start=start, end=end, market=market, refresh=refresh)


def index_daily(code: str, start: int | str | None = None, end: int | str | None = None,
                market: str | None = None, refresh: bool = False) -> Bars:
    """指数日线（独立命名空间，不与个股混装）。"""
    return _store().index_daily(code, start=start, end=end, market=market,
                                refresh=refresh)


def daily_many(codes, start: int | str | None = None, end: int | str | None = None,
               market: str | None = None, namespace: str = "stock",
               refresh: bool = False) -> dict[str, Bars]:
    """批量取多个标的时间序列。**回测请优先用这个** —— 每个分片只装载一次。"""
    return _store().daily_many(codes, start=start, end=end, market=market,
                               namespace=namespace, refresh=refresh)


def cross_section(market: str, date: int | str, sort_by: str = "amount",
                  limit: int | None = None, ascending: bool = False,
                  namespace: str = "stock") -> list[dict]:
    """某一交易日的全市场截面，默认按成交额降序。

    `date` 接受 `20260918` 或 `"2026-09-18"`。
    """
    return _store().cross_section(market, date, sort_by=sort_by, limit=limit,
                                  ascending=ascending, namespace=namespace)


def snapshot() -> str:
    """当前快照 id。记下它 = 记下这次读取的确切输入，随时可复现。"""
    return _store().snapshot


def coverage(market: str) -> dict:
    """该市场的覆盖度声明。`complete=False` 表示这份数据明确知道自己是残缺的。"""
    return _store().coverage(market)


def coverage_note(market: str) -> str:
    return _store().coverage_note(market)


# ── 复权视图 ────────────────────────────────────────────────────────────

def adjust(obj, to: str = "qfq"):
    """把不复权数据换算成复权**视图**。`to` ∈ {`"none"`, `"hfq"`, `"qfq"`}。

    接受 `Bars` 或 pandas DataFrame（后者需含 `af` 列与 `open/high/low/close`）。

        qfq_i = price_i × af_i / af_latest     （锚点 = 最后一个有数据的交易日）
        hfq_i = price_i × af_i / af_scale      （锚点 = 上市首日，af=1）

    **`af_latest` 取「该标的最后一个有数据的交易日的因子」，不是「当前日期的因子」**
    —— 停牌期间不产生新因子。

    港美 V1 没有官方因子源（`af_basis="unavailable"`），此时请求复权**会抛
    `NotSupported`**，而不是退回未复权数据：宁可报错，不给不可信的数字。
    """
    if to not in ADJUSTMENTS:
        raise ValueError(f"to={to!r} 不支持，合法值 {ADJUSTMENTS}")
    if isinstance(obj, Bars):
        return _adjust_bars(obj, to)
    return _adjust_frame(obj, to)


def _adjust_bars(bars: Bars, to: str) -> Bars:
    if to == "none":
        return bars
    if not bars.has_factor or bars.af is None:
        raise NotSupported(
            f"{bars.symbol}: af_basis={bars.af_basis!r}，该市场没有官方复权因子，"
            f"无法给出可信的 {to} 视图（SPEC.md §9.5）")
    af_latest = next((x for x in reversed(bars.af) if x is not None), None)
    scale = bars.af_scale or 1
    out = Bars(
        symbol=bars.symbol, market=bars.market, namespace=bars.namespace,
        price_scale=bars.price_scale, currency=bars.currency,
        volume_unit=bars.volume_unit, af_basis=bars.af_basis,
        af_scale=bars.af_scale, source=bars.source, adjust=to,
        d=list(bars.d), v=list(bars.v), a=list(bars.a), af=list(bars.af),
    )
    for name in ("o", "h", "l", "c"):
        setattr(out, name,
                apply_adjust(getattr(bars, name), bars.af, scale, af_latest, to))
    return out


def _adjust_frame(df, to: str):
    """DataFrame 版本：直接乘因子比例。

    约定 `af` 列是**已归一的因子**（即 `Bars.to_pandas()` 产出的形态：
    上市首日为 1.0），而不是盘上的定点整数。于是：

        hfq: ratio = af            （锚点 = 上市首日）
        qfq: ratio = af / af_last  （锚点 = 最后一个有值的因子）
    """
    if to == "none":
        return df.copy()
    if "af" not in df.columns:
        raise NotSupported(
            "DataFrame 缺少 'af' 列 —— 该市场没有官方复权因子，"
            "无法给出可信的复权视图（SPEC.md §9.5）")
    af = df["af"]
    if af.isna().all():
        raise NotSupported("'af' 列全为空，无法复权")
    ratio = af if to == "hfq" else af / af.dropna().iloc[-1]
    out = df.copy()
    for col in ("open", "high", "low", "close"):
        if col in out.columns:
            out[col] = out[col] * ratio
    return out
