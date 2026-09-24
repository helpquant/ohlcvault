"""ohlcvault 的异常体系。

设计原则：**宁可报错，不给不可信的数字。** 本库所有「数据可能不对」的情形
一律抛异常，绝不静默降级 —— 复权因子缺失时返回未复权数据，比直接报错危险得多。
"""
from __future__ import annotations


class OhlcvaultError(RuntimeError):
    """本库所有异常的基类。"""


class ConfigError(OhlcvaultError):
    """调用方未配置镜像 / 缓存目录等必要参数。"""


class UnknownMarket(OhlcvaultError):
    """市场代码不在契约允许的集合内。"""


class SymbolError(OhlcvaultError, ValueError):
    """证券代码无法归一化。

    同时继承 `ValueError`，便于调用方按惯用法 `except ValueError` 捕获。
    """


class DateError(OhlcvaultError, ValueError):
    """日期参数无法归一化为 `YYYYMMDD` 整数。

    同时继承 `ValueError`，便于调用方按惯用法 `except ValueError` 捕获。
    合法形态见 `store._as_ymd`：`20260918`、`"20260918"`、`"2026-09-18"` 等。
    """


class ContractError(OhlcvaultError):
    """数据违反契约（schema 超版本、数组长度不等、`d` 非递增、串月等）。"""


class SnapshotUnavailable(OhlcvaultError):
    """全部镜像均不可达，且本地无可用缓存。"""


class HashMismatch(OhlcvaultError):
    """取回内容的 sha256 与快照清单不符。"""


class NotSupported(OhlcvaultError):
    """请求的能力在当前数据上不可用。

    典型场景：港美 V1 的 `af_basis="unavailable"`，此时请求前/后复权视图
    **必须**抛这个错，而不是退回未复权价格。
    """
