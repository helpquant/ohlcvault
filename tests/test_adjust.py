"""复权视图 = 纯函数（SPEC.md §9.2）。

重点验证两件事：
1. **全程整数运算** —— 复权结果不能因浮点而依赖平台/实现
2. **无因子时必须报错**，而不是静默退回未复权数据
"""
from __future__ import annotations

import pytest

import ohlcvault as ov
from ohlcvault.adjust import apply_adjust
from ohlcvault.store import Bars

SCALE = 1_000_000


def bars(af=None, af_basis="back_cumulative_from_listing", c=(1000, 2000, 3000)):
    n = len(c)
    return Bars(
        symbol="600519.SH", market="cn", namespace="stock",
        price_scale=1000, currency="CNY", volume_unit="share",
        af_basis=af_basis, af_scale=SCALE if af is not None else None,
        d=[20260901 + i for i in range(n)],
        o=list(c), h=list(c), l=list(c), c=list(c),
        v=[100] * n, a=[1000] * n, af=list(af) if af else None,
    )


def test_hfq_uses_af_scale_as_anchor():
    """后复权锚点是上市首日（af=1.0），所以乘数就是 af/af_scale。"""
    assert apply_adjust([1000], [1_500_000], SCALE, None, "hfq") == [1500]


def test_qfq_uses_last_available_factor():
    """前复权锚点是**最后一个有数据的交易日的因子**，不是「当前日期」。"""
    assert apply_adjust([1000], [1_500_000], SCALE, 3_000_000, "qfq") == [500]


def test_rounding_is_half_up_and_integral():
    """1001 × 1.5 = 1501.5 → 四舍五入 1502（整数路径，无浮点误差）。"""
    assert apply_adjust([1001], [1_500_000], SCALE, None, "hfq") == [1502]


def test_none_is_identity():
    assert apply_adjust([1, 2, 3], [1, 1, 1], SCALE, 1, "none") == [1, 2, 3]


def test_unknown_adjustment_rejected():
    with pytest.raises(ValueError):
        apply_adjust([1], [1], SCALE, 1, "hfq_magic")


def test_qfq_without_factor_raises():
    with pytest.raises(ov.NotSupported):
        apply_adjust([1], [1], SCALE, None, "qfq")


def test_bars_adjust_keeps_lengths_and_marks_view():
    b = bars(af=[1_000_000, 2_000_000, 4_000_000])
    out = ov.adjust(b, to="hfq")
    assert len(out) == len(b)
    assert out.adjust == "hfq"
    assert out.c == [1000, 4000, 12000]
    # 原对象不得被改动 —— 复权是视图，事实层保持不动
    assert b.c == [1000, 2000, 3000]
    assert b.adjust == "none"


def test_bars_adjust_qfq_normalizes_to_latest():
    """前复权 = 原始价 × af_i / af_latest，锚点是**最后一个有数据的交易日的因子**。

    c=(1000,2000,3000)、af=(1e6,2e6,4e6)、af_latest=4e6：
        1000×1e6/4e6 = 250；2000×2e6/4e6 = 1000；3000×4e6/4e6 = 3000
    注意末行必然等于原始价（锚点归一），这正是前复权的定义。
    """
    b = bars(af=[1_000_000, 2_000_000, 4_000_000])
    out = ov.adjust(b, to="qfq")
    assert out.c == [250, 1000, 3000]
    assert out.c[-1] == b.c[-1]              # 锚点处恒等


def test_unavailable_factor_must_raise_not_silently_pass_through():
    """港美 V1 没有官方因子（SPEC.md §9.5）。

    **宁可报错，不给不可信的数字** —— 静默返回未复权数据会让用户以为
    自己拿到的是复权价，这是本库最不能犯的错。
    """
    b = bars(af=None, af_basis="unavailable")
    with pytest.raises(ov.NotSupported):
        ov.adjust(b, to="qfq")
    with pytest.raises(ov.NotSupported):
        ov.adjust(b, to="hfq")


def test_adjust_none_is_allowed_without_factor():
    b = bars(af=None, af_basis="unavailable")
    assert ov.adjust(b, to="none") is b
