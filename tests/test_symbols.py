"""符号规范（SPEC.md §2.4）：必须同时接受两种写法，歧义时**必须报错**。"""
from __future__ import annotations

import pytest

import ohlcvault as ov
from ohlcvault.codes import market_of, normalize, to_api, to_storage


@pytest.mark.parametrize("raw,want", [
    # 后缀式
    ("600519.SH", "sh.600519"),
    ("600519.sh", "sh.600519"),
    ("000001.SZ", "sz.000001"),
    ("000001.SH", "sh.000001"),        # 上证指数，与深市平安银行**不是**同一标的
    ("00700.HK", "hk.00700"),
    ("AAPL.US", "us.AAPL"),
    ("aapl.us", "us.AAPL"),            # 美股代码在存储层必须大写
    # 前缀式
    ("sh.600519", "sh.600519"),
    ("SH.600519", "sh.600519"),
    ("sz000001", "sz.000001"),
    ("us.aapl", "us.AAPL"),
    # 港股零填充到 5 位（港交所规范）
    ("700.HK", "hk.00700"),
    ("hk.700", "hk.00700"),
    ("00005.HK", "hk.00005"),
    # 空白容错
    ("  600519.SH  ", "sh.600519"),
])
def test_normalize(raw, want):
    assert normalize(raw) == want


@pytest.mark.parametrize("raw", ["000001", "600519", "00700"])
def test_bare_code_must_not_be_guessed(raw):
    """裸代码在无市场上下文时**必须报错**。

    猜错交易所拿回来的是一份看起来完全正常的错误数据 —— 比报错危险得多。
    `000001` 在沪市是上证指数、深市是平安银行。
    """
    with pytest.raises(ov.SymbolError):
        normalize(raw)


def test_bare_code_ok_when_market_disambiguates():
    """hk / us 各只有一家交易所，市场上下文足以消歧（SPEC.md §2.4 的例外）。"""
    assert normalize("00700", market="hk") == "hk.00700"
    assert normalize("AAPL", market="us") == "us.AAPL"


def test_bare_code_still_rejected_for_cn():
    """`cn` 有沪深两市，给了 market 也不能消歧。"""
    with pytest.raises(ov.SymbolError):
        normalize("000001", market="cn")


@pytest.mark.parametrize("bad", ["", "  ", "600519.XX", "sh.", "abc.US.extra"])
def test_rejects_malformed(bad):
    with pytest.raises(ov.SymbolError):
        normalize(bad)


def test_non_string_rejected():
    with pytest.raises(ov.SymbolError):
        normalize(600519)  # type: ignore[arg-type]


@pytest.mark.parametrize("api", ["600519.SH", "000001.SZ", "00700.HK", "AAPL.US"])
def test_roundtrip(api):
    assert to_api(to_storage(api)) == api


def test_to_storage_upcases_us():
    assert to_storage("aapl.us") == "us.AAPL"


@pytest.mark.parametrize("storage,market", [
    ("sh.600519", "cn"), ("sz.000001", "cn"), ("bj.430047", "cn"),
    ("hk.00700", "hk"), ("us.AAPL", "us"),
])
def test_market_of(storage, market):
    assert market_of(storage) == market
