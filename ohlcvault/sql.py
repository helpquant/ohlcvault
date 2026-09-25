"""可选扩展：把快照物化进本地 DuckDB，用 SQL 直查行情。

这是「终极方案」的落地形态 —— 但要说清楚它是什么、不是什么：

- **不是数据库服务器**。`to_duckdb()` 在你指定的路径写一个**本地文件**
  （默认在客户端缓存目录内），和分片缓存写 `.gz` 是同一性质 ——
  只是换了一个可以用 SQL 查询、列式压缩的格式。无端口、无服务、无凭据。
- **不改变契约**。DuckDB 文件是从某个快照（sid）派生的本地产物，
  不参与 sha256 校验链；重新 `to_duckdb()` 可随时重建。
- **可选依赖**。`duckdb` 不安装完全不影响核心库（零依赖红线不变）；
  只有用到这里才需要 `pip install ohlcvault[duckdb]`。

典型用法：

    import ohlcvault as ov
    p = ov.to_duckdb(store, markets=["cn"])        # 一次性物化（几分钟）
    con = ov.connect_duckdb(p)                     # 之后任意次 SQL
    con.sql(\"\"\"
        SELECT symbol, d, close/1000.0 AS close
        FROM bars WHERE symbol = '600519.SH'
        ORDER BY d
    \"\"\")
"""
from __future__ import annotations

from pathlib import Path

from .codes import to_api
from .store import Store

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key VARCHAR, value VARCHAR);
CREATE TABLE IF NOT EXISTS bars (
    symbol    VARCHAR,   -- API 式代码，如 '600519.SH'
    market    VARCHAR,   -- 'cn' | 'hk' | 'us'
    namespace VARCHAR,   -- 'stock' | 'index'
    d         BIGINT,    -- YYYYMMDD
    o         BIGINT,    -- 价格 × price_scale（定点整数，契约口径）
    h         BIGINT,
    l         BIGINT,
    c         BIGINT,
    v         BIGINT,    -- 成交量（share）
    a         BIGINT,    -- 成交额；美股可能为 NULL
    af        BIGINT     -- 官方复权因子 × af_scale；港美 V1 为 NULL
);
"""


def _require_duckdb():
    try:
        import duckdb
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "DuckDB 扩展需要 duckdb：pip install 'ohlcvault[duckdb]'"
            "（核心库保持零依赖，此为可选组件）") from e
    return duckdb


def default_path(store: Store) -> Path:
    """默认物化路径：`{cache}/duckdb/{sid}.duckdb` —— 按快照隔离。"""
    return Path(store.client.cache) / "duckdb" / f"{store.snapshot}.duckdb"


def to_duckdb(
    store: Store,
    path: str | Path | None = None,
    markets: list[str] | None = None,
    namespaces: tuple[str, ...] = ("stock", "index"),
    periods: list[str] | None = None,
    refresh: bool = False,
) -> Path:
    """把快照（或其子集）物化成本地 DuckDB 文件，返回文件路径。

    - `markets`：缺省导出全部市场；传 `["cn"]` 只导 A 股
    - `namespaces`：默认 stock + index 都导
    - `periods`：限定月份（如 `["2026-09"]`），小规模试用/测试用
    - `refresh=True`：删掉旧文件重导（同 sid 的文件存在时默认直接复用）

    同一快照重复调用是**幂等**的：文件已存在且 refresh=False 时直接返回。
    """
    duckdb = _require_duckdb()
    for m in (markets or ()):
        Store._check_market(m)
    p = Path(path) if path else default_path(store)
    if p.is_file() and not refresh:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp.duckdb")
    if tmp.exists():
        tmp.unlink()

    con = duckdb.connect(str(tmp))
    try:
        con.execute(_DDL)
        con.executemany("INSERT INTO meta VALUES (?, ?)",
                        [("snapshot", store.snapshot),
                         ("schema", str(store.manifest().get("schema", "")))])
        total = 0
        for rel in store.files():
            parts = rel.split("/")
            if parts[0] != "daily":
                continue
            ns = "index" if parts[1] == "idx" else "stock"
            if ns not in namespaces:
                continue
            market = parts[2 if ns == "index" else 1]
            if markets and market not in markets:
                continue
            period = parts[-1].removesuffix(".json.gz")
            if periods and period not in periods:
                continue
            doc = store.shard(market, period, ns)
            rows = []
            for blk in doc["symbols"]:
                sym = to_api(blk["s"])   # 对外统一 API 式（600519.SH）
                d, o, h, l, c, v, a = (blk[k] for k in "dohlcva")
                af = blk.get("af")
                n = len(d)
                for i in range(n):
                    rows.append((sym, market, ns, d[i], o[i], h[i], l[i],
                                 c[i], v[i], a[i],
                                 af[i] if af is not None else None))
                # 分批写入，避免单分片行数过大撑爆内存
                if len(rows) >= 50_000:
                    con.executemany(
                        "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows)
                    total += len(rows)
                    rows.clear()
            if rows:
                con.executemany(
                    "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows)
                total += len(rows)
        con.execute("CREATE INDEX IF NOT EXISTS bars_sym_d ON bars (symbol, d)")
    finally:
        con.close()
    tmp.replace(p)
    return p


def connect_duckdb(path: str | Path, read_only: bool = True):
    """打开一个已物化的 DuckDB 文件，返回 duckdb 连接（SQL 随意写）。"""
    duckdb = _require_duckdb()
    return duckdb.connect(str(path), read_only=read_only)
