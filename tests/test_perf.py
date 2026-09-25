"""性能层测试：keep-alive 连接池、按标的物化缓存、DuckDB 扩展。

与既有测试同一哲学：用真实分片（conftest 的 data 根），不造合成数据。
"""
from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

import ohlcvault as ov
from ohlcvault.net import HTTPPool


# ── HTTP keep-alive 连接池 ──────────────────────────────────────────────

class _QuietHandler(http.server.BaseHTTPRequestHandler):
    """把目录当根的静态服务；记录每个请求的连接端口供复用断言。"""

    server_version = "TestHTTP/1.0"

    def do_GET(self):  # noqa: N802
        root: Path = self.server.root  # type: ignore[attr-defined]
        p = root / self.path.lstrip("/")
        if not p.is_file():
            self.send_error(404)
            return
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静默
        pass


@pytest.fixture()
def http_mirror(data_dir, tmp_path):
    """把真实数据根通过 HTTP 暴露（模拟远程镜像），返回 base URL。"""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
    srv.root = data_dir  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_pool_reuses_connection(http_mirror):
    """同一 host 连续 GET 必须复用连接（fresh=1），且字节正确。"""
    pool = HTTPPool(timeout=10)
    b1, s1 = pool.get(f"{http_mirror}/latest.json")
    b2, s2 = pool.get(f"{http_mirror}/latest.json")
    assert (s1, s2) == (200, 200)
    assert json.loads(b1) == json.loads(b2)
    assert pool.stats["fresh"] == 1          # 只建了一次连接
    assert pool.stats["reused"] >= 1
    pool.close()


def test_pool_handles_404(http_mirror):
    pool = HTTPPool(timeout=10)
    _, status = pool.get(f"{http_mirror}/no/such/file.gz")
    assert status == 404
    pool.close()


def test_client_over_http(http_mirror, tmp_path):
    """MarketClient 经 HTTP 池取分片并通过 sha256 校验。"""
    cli = ov.MarketClient([http_mirror], tmp_path / "cache")
    mf = cli.manifest()                     # latest.json → 快照清单
    assert mf["snapshot"]
    rel = next(r for r in cli.files() if r.startswith("daily/"))
    body = cli.raw(rel)
    assert body[:2] == b"\x1f\x8b"          # 真实 gzip 分片
    # 二读应命中本地缓存
    n0 = cli.stats["cache_hit"]
    cli.raw(rel)
    assert cli.stats["cache_hit"] == n0 + 1


# ── shard_cache 默认值与 O(1) block 索引 ────────────────────────────────

def test_shard_cache_default(store):
    assert store._shard_cache_max == 24


def test_block_index_built(store):
    """shard() 装载分片后必须建好 O(1) 索引。"""
    periods = store.available_periods("cn")
    store.shard("cn", periods[-1])
    rel = f"daily/cn/{periods[-1]}.json.gz"
    idx = store._block_index.get(rel)
    assert idx is not None and len(idx) > 0
    doc = store.shard("cn", periods[-1])
    for blk in doc["symbols"][:5]:
        assert idx[blk["s"]] is blk


# ── 按标的物化缓存 ──────────────────────────────────────────────────────

@pytest.fixture()
def store_factory(data_dir, tmp_path):
    """按需构造带独立缓存目录的 Store（可传 materialize 等参数）。"""
    counter = {"n": 0}

    def make(**kw):
        counter["n"] += 1
        return ov.connect(mirrors=[str(data_dir)],
                          cache_dir=tmp_path / f"cache{counter['n']}",
                          quiet=True, **kw)

    return make


def test_materialize_roundtrip(store):
    """全历史读两次：第一次走分片，第二次必须命中物化缓存且结果一致。"""
    syms = store.symbols("cn", type="stock")
    code = next(e["api"] for e in syms if e["status"] == "listed")
    b1 = store.daily(code)
    assert store.stats["materialize_write"] >= 1
    assert store.stats["materialize_hit"] == 0

    # 断掉分片来源：二读若还碰分片就直接炸，证明走的是物化文件
    orig_shard = store.shard
    store.shard = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("二读不应触碰分片"))
    try:
        b2 = store.daily(code)
    finally:
        store.shard = orig_shard
    assert list(b2.d) == list(b1.d)
    assert list(b2.c) == list(b1.c)
    assert list(b2.af or []) == list(b1.af or [])
    assert b2.symbol == b1.symbol
    assert store.stats["materialize_hit"] == 1


def test_materialize_slice_from_cache(store):
    """带 start/end 的读取在物化命中时做二次切片，结果与分片路径一致。"""
    syms = store.symbols("cn", type="stock")
    code = next(e["api"] for e in syms if e["status"] == "listed")
    full = store.daily(code)                       # 触发物化
    assert len(full.d) > 0
    mid = full.d[len(full.d) // 2]
    sliced = store.daily(code, start=mid, end=mid)  # 单日切片
    assert sliced.d == [mid]
    ref = full.d.index(mid)
    assert sliced.c == [full.c[ref]]


def test_materialize_invalidates_on_other_snapshot(store):
    """物化文件落在「当前快照 id」目录下（换快照自然不命中旧缓存）。"""
    code = next(e["api"] for e in store.symbols("cn", type="stock"))
    store.daily(code)
    storage = ov.to_storage(code)                 # 文件名用存储式符号（sh.600519）
    mat_file = (Path(store.client.cache) / "symbols" / store.snapshot
                / "stock" / f"{storage}.json.gz")
    assert store.stats["materialize_write"] >= 1
    assert mat_file.exists()


def test_daily_many_materialized(store):
    """daily_many 二读全走物化缓存。"""
    # 必须选「有数据」的在市股：退市股可能 0 行，0 行标的本就不物化
    syms = [e["api"] for e in store.symbols("cn", type="stock", status="listed")][:3]
    r1 = store.daily_many(syms)
    assert len(r1) == 3
    assert all(len(b.d) > 0 for b in r1.values())
    orig_shard = store.shard
    store.shard = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("二读不应触碰分片"))
    try:
        r2 = store.daily_many(syms)
    finally:
        store.shard = orig_shard
    for k in r1:
        assert list(r2[k].d) == list(r1[k].d)


def test_materialize_disabled(store_factory):
    """materialize=False 时不写物化文件（尊重调用方选择）。"""
    store = store_factory(materialize=False)
    code = next(e["api"] for e in store.symbols("cn", type="stock"))
    store.daily(code)
    assert store.stats["materialize_write"] == 0


# ── DuckDB 扩展 ─────────────────────────────────────────────────────────

duckdb = pytest.importorskip("duckdb")


def test_to_duckdb_roundtrip(store, tmp_path):
    """小范围物化 → SQL 查询 → 与客户端读取一致。"""
    p = ov.to_duckdb(store, path=tmp_path / "t.duckdb",
                     markets=["cn"], periods=["2026-09"])
    con = ov.connect_duckdb(p)
    row = con.sql("""
        SELECT d, o, h, l, c FROM bars
        WHERE symbol = '600519.SH' ORDER BY d DESC LIMIT 1
    """).fetchone()
    bars = store.daily("600519.SH", start=20260901, end=20260930)
    assert row is not None
    assert row[0] == bars.d[-1] and row[4] == bars.c[-1]
    # 幂等：同参数二调直接返回
    assert ov.to_duckdb(store, path=p, markets=["cn"],
                        periods=["2026-09"]) == p
    con.close()
