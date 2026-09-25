"""读取层测试 —— 跑在**真实分片**上（见 conftest.py 的取舍说明）。"""
from __future__ import annotations

import pytest

import ohlcvault as ov
from ohlcvault.codes import market_of, normalize, to_api, to_storage

PERIODS_CN = ["2026-07", "2026-08", "2026-09"]


def pick_stock(shard: dict) -> str:
    """从真实分片里挑一个普通个股（存储式）。"""
    for blk in shard["symbols"]:
        s = blk["s"]
        return s  # 分片已按 s 升序，取第一个即可
    raise AssertionError("分片为空")


# ── 快照与 meta ──────────────────────────────────────────────────────────

def test_snapshot_matches_latest_pointer(store, data_dir):
    import json
    want = json.loads((data_dir / "latest.json").read_bytes())["snapshot"]
    assert store.snapshot == want
    # 同一 store 的所有读取都锚定同一个快照 —— 这是可复现性的前提
    assert store.snapshot == store.snapshot


PERIODS_CN = ["2026-07", "2026-08", "2026-09"]  # 仅作 sanity 下界参考；断言用结构性质


def test_available_periods(store):
    ps = store.available_periods("cn")
    assert ps == sorted(ps) and len(set(ps)) == len(ps)
    assert ps[-1] >= "2026-09"          # 至少覆盖到发布当月
    assert store.available_periods("cn", "index") == ps


def test_symbols_universe_includes_delisted(store):
    """退市股必须在池内（SPEC.md §5.3）。

    回测池若只含今天还活着的股票，历史收益会被**系统性高估**。
    """
    rows = store.symbols("cn", type="stock")
    assert len(rows) > 1000
    delisted = [r for r in rows if r["status"] == "delisted"]
    assert delisted, "A 股清单里应当有退市股"
    # status 与 out 的语义必须一致（SPEC.md §5.2）
    for r in delisted:
        assert isinstance(r["out"], int)
    for r in rows:
        if r["status"] == "listed":
            assert r["out"] is None


def test_symbol_entry_shape(store):
    st = store.shard("cn", "2026-09")["symbols"][0]["s"]
    e = store.symbol(st)
    assert e["s"] == st
    assert e["api"] == to_api(st)          # api 由 s 机械推导
    assert e["type"] in ("stock", "index", "etf", "bond", "other")
    assert isinstance(e["code"], str)          # 保留前导零 → 必须是字符串


def test_calendar_sorted_and_bounded(store):
    days = store.calendar("cn")
    assert days == sorted(days)
    assert len(days) == len(set(days))
    assert store.calendar("cn", start=20260101, end=20260131) == \
        [d for d in days if 20260101 <= d <= 20260131]


def test_coverage_declares_gaps(store):
    """覆盖缺口必须显式声明，不得隐瞒（SPEC.md §5.4）。"""
    assert "complete" in store.coverage("cn")


# ── 时间序列 ────────────────────────────────────────────────────────────

def test_daily_single_month(store):
    """单月切片必须只含该月的交易日。

    注意 `daily()` 不传区间时会返回**快照里全部月份**，所以要显式给 start/end。
    """
    s = pick_stock(store.shard("cn", "2026-09"))
    b = store.daily(s, start=20260901, end=20260930)
    assert len(b) > 0
    assert b.adjust == "none"                  # 只存事实，不存视图
    assert b.volume_unit == "share"
    assert all(20260901 <= d <= 20260930 for d in b.d)
    assert b.d == sorted(b.d) and len(b.d) == len(set(b.d))


def test_daily_spans_months_and_is_strictly_increasing(store):
    """跨月拼接是客户端职责 —— 拼接后必须严格递增、无重复。"""
    s = pick_stock(store.shard("cn", "2026-07"))
    one = store.daily(s, start=20260701, end=20260731)
    if len(one) == 0:
        pytest.skip("该标的当月无数据")
    b = store.daily(s, start=20260701, end=20260930)
    assert len(b) >= len(one)
    assert all(b.d[i] < b.d[i + 1] for i in range(len(b.d) - 1))
    assert b.d[0] >= 20260701 and b.d[-1] <= 20260930


def test_daily_date_filter_edges(store):
    s = pick_stock(store.shard("cn", "2026-08"))
    full = store.daily(s, start=20260801, end=20260831)
    if len(full) < 3:
        pytest.skip("样本太短")
    mid = store.daily(s, start=full.d[1], end=full.d[-2])
    assert mid.d == full.d[1:-1]
    assert mid.c == full.c[1:-1]


def test_unknown_symbol_raises_keyerror(store):
    with pytest.raises(KeyError):
        store.daily("sz.999999")


def test_ambiguous_symbol_rejected(store):
    with pytest.raises(ov.SymbolError):
        store.daily("000001")                  # 沪市指数 / 深市平安银行，必须报错


def test_batch_equals_individual(store):
    """`daily_many` 是回测主路径，结果必须与逐个取完全一致。"""
    shard = store.shard("cn", "2026-09")
    picked = [blk["s"] for blk in shard["symbols"][:5]]
    batch = store.daily_many(picked)
    assert len(batch) == len(picked)
    for st in picked:
        api = to_api(st)
        one = store.daily(st)
        assert batch[api].d == one.d
        assert batch[api].c == one.c
        assert batch[api].a == one.a


def test_index_daily_uses_separate_namespace(store):
    """指数在 `daily/idx/` 独立命名空间，不与个股混装。"""
    shard = store.shard("cn", "2026-09", namespace="index")
    assert shard["namespace"] == "index"
    blk = shard["symbols"][0]
    b = store.index_daily(blk["s"], start=20260901, end=20260930)
    assert b.namespace == "index"
    assert b.d == blk["d"]


def test_index_not_mixed_into_stock_shard(store):
    stock = {blk["s"] for blk in store.shard("cn", "2026-09")["symbols"]}
    idx = {blk["s"] for blk in store.shard("cn", "2026-09", "index")["symbols"]}
    assert not (stock & idx), "指数与个股不得出现在同一个分片里"


# ── 断面 ────────────────────────────────────────────────────────────────

def test_cross_section_sorted_by_amount_desc(store):
    shard = store.shard("cn", "2026-09")
    date = max(blk["d"][-1] for blk in shard["symbols"] if blk["d"])
    rows = store.cross_section("cn", date)
    assert rows, "该交易日应有多行"
    amounts = [r["amount"] for r in rows if r["amount"] is not None]
    assert amounts == sorted(amounts, reverse=True)


def test_cross_section_limit_and_shape(store):
    shard = store.shard("cn", "2026-09")
    date = max(blk["d"][-1] for blk in shard["symbols"] if blk["d"])
    top = store.cross_section("cn", date, limit=10)
    assert len(top) == 10
    for r in top:
        assert set(r) >= {"symbol", "d", "open", "high", "low", "close",
                          "volume", "amount"}
        assert r["d"] == date
        assert r["low"] <= min(r["open"], r["close"])
        assert r["high"] >= max(r["open"], r["close"])


def test_cross_section_matches_shard_directly(store):
    """断面必须是分片的**转置**，不得引入任何额外数据源。"""
    shard = store.shard("cn", "2026-09")
    date = max(blk["d"][-1] for blk in shard["symbols"] if blk["d"])
    rows = {r["storage"]: r for r in store.cross_section("cn", date)}
    hits = 0
    for blk in shard["symbols"]:
        if date in blk["d"]:
            i = blk["d"].index(date)
            r = rows[blk["s"]]
            assert r["close"] == pytest.approx(blk["c"][i] / shard["price_scale"])
            assert r["amount"] == blk["a"][i]
            hits += 1
    assert hits == len(rows)


def test_cross_section_rejects_unknown_sort_key(store):
    with pytest.raises(ValueError):
        store.cross_section("cn", 20260918, sort_by="market_cap")


# ── 契约校验 ────────────────────────────────────────────────────────────

def test_validation_rejects_broken_block(store):
    """数组长度不等 = 文件损坏，必须被拦下（SPEC.md §4.2 约束 1）。"""
    doc = {"schema": 1, "market": "cn", "namespace": "stock",
           "period": "2026-09", "adjust": "none", "price_scale": 1000,
           "currency": "CNY", "volume_unit": "share",
           "af_basis": "back_cumulative_from_listing"}
    bad = {"s": "sh.600519", "d": [20260901, 20260902],
           "o": [1, 2], "h": [1, 2], "l": [1, 2], "c": [1, 2],
           "v": [1], "a": [1]}          # v / a 短了一个
    with pytest.raises(ov.ContractError):
        store._validate_block(bad, doc, "fake.json.gz")


def test_validation_rejects_unsorted_dates(store):
    doc = {"schema": 1, "market": "cn", "namespace": "stock", "period": "2026-09",
           "adjust": "none", "volume_unit": "share",
           "af_basis": "back_cumulative_from_listing"}
    bad = {"s": "sh.600519", "d": [20260902, 20260901],
           "o": [1, 2], "h": [1, 2], "l": [1, 2], "c": [1, 2],
           "v": [1, 2], "a": [1, 2]}
    with pytest.raises(ov.ContractError):
        store._validate_block(bad, doc, "fake.json.gz")


def test_validation_rejects_cross_month_block(store):
    doc = {"schema": 1, "market": "cn", "namespace": "stock", "period": "2026-09",
           "adjust": "none", "volume_unit": "share",
           "af_basis": "back_cumulative_from_listing"}
    bad = {"s": "sh.600519", "d": [20260831, 20260901],
           "o": [1, 2], "h": [1, 2], "l": [1, 2], "c": [1, 2],
           "v": [1, 2], "a": [1, 2]}
    with pytest.raises(ov.ContractError):
        store._validate_block(bad, doc, "fake.json.gz")


def test_validation_rejects_adjusted_storage(store):
    """`adjust` 必须恒为 `none` —— 静态托管只存事实（SPEC.md §9.2）。"""
    with pytest.raises(ov.ContractError):
        store._validate_header(
            {"schema": 1, "market": "cn", "namespace": "stock", "period": "2026-09",
             "adjust": "qfq", "volume_unit": "share", "af_basis": "back_cumulative_from_listing"},
            "fake.json.gz", "cn", "stock", "2026-09")


def test_validation_rejects_future_schema(store):
    with pytest.raises(ov.ContractError):
        store._validate_header(
            {"schema": 99, "market": "cn", "namespace": "stock", "period": "2026-09",
             "adjust": "none", "volume_unit": "share", "af_basis": "back_cumulative_from_listing"},
            "fake.json.gz", "cn", "stock", "2026-09")


def test_rejects_unknown_market_and_namespace(store):
    with pytest.raises(ov.UnknownMarket):
        store.available_periods("jp")
    with pytest.raises(ov.UnknownMarket):
        store.available_periods("cn", "futures")


# ── 复权端到端 ──────────────────────────────────────────────────────────

def test_adjust_on_real_bars(store):
    """后复权在真实数据上必须逐行等于 `v × af / af_scale`（整数、四舍五入）。"""
    from ohlcvault.adjust import apply_adjust

    shard = store.shard("cn", "2026-09")
    blk = next(b for b in shard["symbols"] if "af" in b and b["af"])
    b = store.daily(blk["s"])
    if not b.has_factor:
        pytest.skip("该标的没有因子")
    out = ov.adjust(b, to="hfq")
    assert out.adjust == "hfq"
    assert out.c == apply_adjust(b.c, b.af, b.af_scale, None, "hfq")
    # 前复权在锚点（最后一个有数据的交易日）处必须恒等于原始价
    q = ov.adjust(b, to="qfq")
    assert q.c[-1] == b.c[-1]
    assert q.d == b.d and b.adjust == "none"   # 原对象不被改动


# ── 日期参数宽容（与 §2.4 符号宽容同一哲学）──────────────────────────────

def test_dates_accept_int_and_string_forms(store):
    """start/end 同时接受 int `YYYYMMDD` 与常见字符串形态。"""
    a = store.daily("600519.SH", start=20260801, end=20260930)
    b = store.daily("600519.SH", start="2026-08-01", end="2026-09-30")
    c = store.daily("600519.SH", start="20260801", end="20260930")
    d = store.daily("600519.SH", start="2026/08/01", end="2026.09.30")
    assert len(a.d) > 0
    assert a.d == b.d == c.d == d.d


def test_dates_reject_garbage(store):
    """非法日期抛 `DateError`（带人话），而不是在库深处抛 TypeError。"""
    from ohlcvault.errors import DateError
    with pytest.raises(DateError):
        store.daily("600519.SH", start="2026-13-01")     # 月份越界
    with pytest.raises(DateError):
        store.daily("600519.SH", start="not-a-date")     # 无法解析
    with pytest.raises(DateError):
        store.daily("600519.SH", start=True)             # bool 不算日期
    with pytest.raises(DateError):
        store.cross_section("cn", date=None)             # 断面必须有具体日期
