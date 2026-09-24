"""证券代码双轨规范（SPEC.md §2）。

| 场合 | 形态 |
|---|---|
| 存储层（文件、清单、路径） | 前缀式小写 `sh.600519` / `us.AAPL` |
| 对外 API（入参、返回值） | 后缀式大写 `600519.SH` / `AAPL.US` |

**模块名叫 `codes` 而不是 `symbols`**：包门面里有一个同名的
`ohlcvault.symbols(market=...)` 函数（返回标的清单），
同名模块与同名函数在 `__init__` 里必然互相遮蔽，改掉模块名是最省事的解法。
顶层已把常用函数直接导出（`ov.to_api` / `ov.to_storage` / `ov.normalize_symbol`），
所以使用者通常不需要直接导入本模块。

**用户永远不需要记规则。** 本模块接受任意常见写法并归一化；但遇到
真正有歧义的输入（如裸 `000001`，沪市是上证指数、深市是平安银行）
**必须报错而非猜测** —— 猜错一个交易所，拿回来的是一份看起来完全正常的
错误数据，比报错危险得多。
"""
from __future__ import annotations

from .errors import SymbolError

# 交易所代码（SPEC.md §2.2）
EXCHANGES = ("sh", "sz", "bj", "hk", "us")

MARKET_EXCHANGES: dict[str, tuple[str, ...]] = {
    "cn": ("sh", "sz", "bj"),
    "hk": ("hk",),
    "us": ("us",),
}

# 仅当 market 上下文已知时可推断交易所的市场（SPEC.md §2.4 的例外）
_CONTEXT_INFERABLE = {"hk": "hk", "us": "us"}


def to_api(storage: str) -> str:
    """存储式 → API 式。`sh.600519` → `600519.SH`，`us.AAPL` → `AAPL.US`。"""
    ex, code = storage.split(".", 1)
    return f"{code.upper() if ex == 'us' else code}.{ex.upper()}"


def to_storage(api: str) -> str:
    """API 式 → 存储式。`600519.SH` → `sh.600519`，`AAPL.US` → `us.AAPL`。

    美股代码在存储层是**大写**（SPEC.md §2.1），所以 `aapl.us` 也要归一成 `us.AAPL`。
    """
    code, ex = api.rsplit(".", 1)
    exl = ex.lower()
    if exl not in EXCHANGES:
        raise SymbolError(f"无法识别的交易所后缀：{api!r}（合法值 {EXCHANGES}）")
    return f"{exl}.{code.upper() if exl == 'us' else code}"


def normalize(raw: str, market: str | None = None) -> str:
    """把用户可能写出的任意形态归一化为**存储式**（SPEC.md §2.4）。

    >>> normalize("600519.SH")
    'sh.600519'
    >>> normalize("sh600519")
    'sh.600519'
    >>> normalize("00700.HK")
    'hk.00700'
    >>> normalize("AAPL")
    Traceback (most recent call last):
    ...
    ohlcvault.errors.SymbolError: ...

    `market` 只在 `hk` / `us` 下提供消歧能力（这两个市场各只有一家交易所）；
    对 `cn` 提供 `market="cn"` 也**不会**让裸 `000001` 通过 —— 它分属沪深两市。
    """
    if not isinstance(raw, str):
        raise SymbolError(f"symbol 必须是字符串，收到 {type(raw).__name__}")
    s = raw.strip()
    if not s:
        raise SymbolError("空的 symbol")

    low = s.lower()

    # ① 前缀式：sh.600519 / sh600519
    for ex in EXCHANGES:
        if low.startswith(ex + "."):
            return _canonical(ex, s[len(ex) + 1:], raw)
        if low.startswith(ex) and "." not in s and low[len(ex):].isalnum():
            return _canonical(ex, s[len(ex):], raw)

    # ② 后缀式：600519.SH
    if "." in s:
        code, ex = s.rsplit(".", 1)
        exl = ex.lower()
        if exl in EXCHANGES:
            return _canonical(exl, code, raw)
        raise SymbolError(f"无法识别的交易所后缀：{raw!r}（合法值 {EXCHANGES}）")

    # ③ 裸代码：仅在市场上下文唯一确定交易所时才允许
    infer = _CONTEXT_INFERABLE.get(market or "")
    if infer:
        return _canonical(infer, s, raw)

    raise SymbolError(
        f"symbol 缺少交易所信息：{raw!r}。请写成 '600519.SH' 或 'sh.600519'。"
        f"（契约禁止猜测交易所 —— 000001 在沪市是上证指数、深市是平安银行）"
    )


def _canonical(exchange: str, code: str, raw: str) -> str:
    if not code:
        raise SymbolError(f"symbol 缺少代码部分：{raw!r}")
    # 港股代码必须零填充到 5 位（港交所规范，也是 akshare / yfinance 的共同要求）
    if exchange == "hk":
        code = code.zfill(5)
    code = code.upper() if exchange == "us" else code
    return f"{exchange}.{code}"


def split(storage: str) -> tuple[str, str]:
    """存储式 → (交易所小写, 代码)。"""
    ex, code = storage.split(".", 1)
    return ex, code


def market_of(storage: str) -> str:
    """存储式 → 市场代码。"""
    ex = storage.split(".", 1)[0]
    for mkt, exs in MARKET_EXCHANGES.items():
        if ex in exs:
            return mkt
    raise SymbolError(f"未知交易所：{storage!r}")
