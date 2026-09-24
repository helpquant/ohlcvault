"""复权视图 = 纯函数（SPEC.md §9.2）。

**这是本数据集的第一原则的落地点：只存事实，不存视图。**

复权价不是事实，是「相对于某个锚点重算出来的视图」。前复权的锚点是**最新价**，
所以每次分红送转都会导致**全历史价格被重算** —— 若把前复权价存进静态文件，
历史文件就会天天变，`immutable` 缓存全部失效，整套免费分发的收益归零。

因此：盘上只存不复权原始价 + 官方后复权累计因子 `af`，
前/后复权都在客户端由下面这两个纯函数算出来。

    后复权 hfq_i = c_i × af_i / af_scale      （锚点 = 上市首日，af=1）
    前复权 qfq_i = c_i × af_i / af_latest     （锚点 = 最后一个有数据的交易日）

**全部用整数运算**（`price_scale` 与 `af_scale` 都是整数标度）。
引入浮点会让同一份数据在不同实现/架构上产生不同的末位，破坏可复现性 ——
这与「数据集里不放浮点数」是同一条纪律。
"""
from __future__ import annotations

from .errors import NotSupported

ADJUSTMENTS = ("none", "hfq", "qfq")


def _div_round(num: int, den: int) -> int:
    """整数除法，四舍五入（half-up）。只用整数，结果与平台无关。"""
    return (2 * num + den) // (2 * den)


def adjust_factor(af: int, af_scale: int, af_latest: int | None, to: str) -> float:
    """给定某日的因子，返回该日价格的乘数（浮点，仅供外部参考）。"""
    if to == "hfq":
        return af / af_scale
    if to == "qfq":
        if not af_latest:
            raise NotSupported("qfq 需要 af_latest，但序列没有可用因子")
        return af / af_latest
    return 1.0


def apply_adjust(
    values: list[int],
    af: list[int],
    af_scale: int,
    af_latest: int | None,
    to: str,
) -> list[int]:
    """把一组定点整数价格按因子换算成复权视图，仍返回同标度的定点整数。

    `to="none"` 原样返回。`af` 与 `values` 必须等长（契约 §4.2 硬性约束 1）。
    """
    if to == "none":
        return list(values)
    if to == "hfq":
        return [_div_round(v * f, af_scale) for v, f in zip(values, af)]
    if to == "qfq":
        if not af_latest:
            raise NotSupported("qfq 需要 af_latest，但序列没有可用因子")
        return [_div_round(v * f, af_latest) for v, f in zip(values, af)]
    raise ValueError(f"未知复权类型 {to!r}，合法值 {ADJUSTMENTS}")
