"""读取层：把月分片拼成时间序列 / 断面，并做契约校验。

分片策略决定了这里的算法：**一个市场一个月一个文件**，装着该市场全部标的在
该月的全部交易日。于是：

- 取 10 年日线 = 拉 ~120 个月文件，每个文件都是「一次读取，多标的共享」
- 取某日全市场截面 = 读**一个**文件再转置（实测 2.8 ms 量级）——
  这就是「不需要为断面额外派生一份数据」的全部理由
- 批量回测应优先用 `daily_many()`：一次装载，多标的提取，避免 N 倍重复解压

**跨月拼接是客户端职责**，因为服务端只存事实、不做视图。
"""
from __future__ import annotations

import gzip
import json
import re
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .codes import market_of, normalize, to_api
from .adjust import apply_adjust
from .client import MarketClient
from .config import AF_SCALE, MARKETS, NAMESPACES, PRICE_SCALE, SUPPORTED_SCHEMA
from .errors import ContractError, DateError, NotSupported, UnknownMarket

_SHARD_RE = re.compile(r"^daily/(?:idx/)?(?P<market>[a-z]{2})/(?P<period>\d{4}-\d{2})\.json\.gz$")

# 断面可按哪些字段排序。注意这里是**返回行**的键名（`amount`/`volume`…），
# 不是 block 里的单字母键（`a`/`v`…）—— 两者不同，映射错了排序会静默全落到 None。
_SORTABLE = ("amount", "volume", "close", "open", "high", "low", "pct_chg")


def _sort_none_last(rows: list[dict], key: str, ascending: bool) -> list[dict]:
    """按 `key` 排序，**缺失值恒排最后**（与升降序无关）。

    直接 `sort(reverse=True)` 会把 None 顶到最前面 —— 那会让「停牌/无成交额」
    的标的占据榜单头部，是比排序错误更难发现的显示错误。Python 排序稳定，
    所以拆成「有值部分排序 + 无值部分追加」即可。
    """
    have = [r for r in rows if r.get(key) is not None]
    miss = [r for r in rows if r.get(key) is None]
    have.sort(key=lambda r: r[key], reverse=not ascending)
    return have + miss


def period_of(d: int) -> str:
    """20260918 → '2026-09'"""
    return f"{d // 10000:04d}-{d // 100 % 100:02d}"


def periods_between(start: int, end: int) -> list[str]:
    """[20260901, 20261231] → ['2026-09','2026-10','2026-11','2026-12']"""
    y0, m0 = start // 10000, start // 100 % 100
    y1, m1 = end // 10000, end // 100 % 100
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _as_ymd(value, name: str) -> int | None:
    """把调用方给出的日期归一为整数 `YYYYMMDD`（SPEC.md §9.6 的落盘口径）。

    与符号规范（§2.4「必须同时接受多种写法」）同一哲学：接口宽容、内核统一。
    接受 `20260918` / `"20260918"` / `"2026-09-18"` / `"2026/09/18"` / `"2026.09.18"`；
    其余一律抛 `DateError` —— 宁可报错，不静默猜。
    """
    if value is None:
        return None
    d: int | None = None
    if isinstance(value, bool):
        pass
    elif isinstance(value, int):
        d = value
    elif isinstance(value, float) and value.is_integer():
        d = int(value)
    elif isinstance(value, str):
        s = (value.strip().replace("-", "").replace("/", "")
             .replace(".", "").replace(" ", ""))
        if s.isdigit() and len(s) == 8:
            d = int(s)
    if d is None:
        raise DateError(
            f"{name}={value!r} 不是合法日期。"
            "接受 20260918 / \"20260918\" / \"2026-09-18\" / \"2026/09/18\"")
    y, m, dd = d // 10000, d // 100 % 100, d % 100
    if not (1900 <= y <= 2999 and 1 <= m <= 12 and 1 <= dd <= 31):
        raise DateError(f"{name}={value!r} 的年月日越界（{y}-{m}-{dd}）")
    return d


@dataclass(slots=True)
class Bars:
    """一个标的的时间序列。并行数组形态，与盘上的列式存储一一对应。

    **保留定点整数**（`o/h/l/c` 是价格 × `price_scale`）。想拿浮点价用
    `to_pandas()`，想做复权用 `ohlcvault.adjust()`。整数是契约的一部分，
    在库内不做隐式转换 —— 一旦转成浮点就再也回不到「逐字节可复现」。
    """

    symbol: str                     # API 式，如 '600519.SH'
    market: str
    namespace: str                  # 'stock' | 'index'
    price_scale: int
    currency: str
    volume_unit: str
    af_basis: str
    d: list[int] = field(default_factory=list)
    o: list[int] = field(default_factory=list)
    h: list[int] = field(default_factory=list)
    l: list[int] = field(default_factory=list)
    c: list[int] = field(default_factory=list)
    v: list[int | None] = field(default_factory=list)
    a: list[int | None] = field(default_factory=list)
    af: list[int | None] | None = None      # None = 本市场无因子（SPEC §9.5）
    af_scale: int | None = AF_SCALE
    source: str = ""
    adjust: str = "none"

    def __len__(self) -> int:
        return len(self.d)

    def __repr__(self) -> str:
        if not self.d:
            return f"<Bars {self.symbol} 空>"
        return (f"<Bars {self.symbol} {self.d[0]}…{self.d[-1]} "
                f"rows={len(self.d)} adjust={self.adjust}>")

    @property
    def has_factor(self) -> bool:
        return self.af is not None and self.af_basis != "unavailable"

    def last_index(self) -> int:
        return len(self.d) - 1

    def rows(self) -> Iterator[dict]:
        """逐行 dict（行式，便于喂给 LLM / 写 CSV）。"""
        for i in range(len(self.d)):
            yield {
                "symbol": self.symbol,
                "d": self.d[i],
                "open": self.o[i] / self.price_scale,
                "high": self.h[i] / self.price_scale,
                "low": self.l[i] / self.price_scale,
                "close": self.c[i] / self.price_scale,
                "volume": self.v[i],
                "amount": self.a[i],
                "af": (self.af[i] / self.af_scale
                       if self.af is not None and self.af_scale else None),
            }

    def to_pandas(self):
        """转 DataFrame（需要 pandas）。行情列为浮点，索引为 DatetimeIndex。"""
        try:
            import pandas as pd
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "to_pandas() 需要 pandas：pip install ohlcvault[pandas]") from e
        ps = self.price_scale
        df = pd.DataFrame({
            "open": [x / ps for x in self.o],
            "high": [x / ps for x in self.h],
            "low": [x / ps for x in self.l],
            "close": [x / ps for x in self.c],
            "volume": self.v,
            "amount": self.a,
            "af": ([x / self.af_scale for x in self.af]
                   if self.af is not None and self.af_scale else None),
        }, index=pd.to_datetime([str(x) for x in self.d], format="%Y%m%d"))
        df.index.name = "date"
        return df


class Store:
    """按快照读取数据。构造后所有读取都针对**同一个快照 id**，天然可复现。"""

    def __init__(self, client: MarketClient, validate: bool = True,
                 shard_cache: int = 4):
        self.client = client
        self.validate = validate
        self._shard_cache: OrderedDict[str, dict] = OrderedDict()
        self._shard_cache_max = max(1, shard_cache)
        self._meta_cache: dict[str, dict] = {}

    # ---------- 基础 ----------

    @property
    def snapshot(self) -> str:
        """当前快照 id。把它记下来就能在任何时候复现这次读取。"""
        return self.client.snapshot

    def files(self) -> list[str]:
        return self.client.files()

    def coverage(self, market: str) -> dict:
        """该市场的覆盖度声明（SPEC.md §5.4）。

        `complete=False` 意味着**这份数据明确知道自己是残缺的**。
        一个数据项目隐瞒覆盖缺口，比覆盖不全更严重 —— 所以 `open()` 会在
        首次使用时把它打到日志里，而不是让它静静地躺在 JSON 里。
        """
        self._check_market(market)
        doc = self._meta(f"meta/symbols/{market}.json")
        return doc.get("coverage") or {}

    def manifest(self) -> dict:
        return self.client.manifest()

    def _json(self, rel: str, refresh: bool = False):
        """取一个可能是 gzip 的 JSON。

        **校验的对象是盘上的字节**（`.gz`），解压发生在校验之后 ——
        这正是为什么分发端绝不能设 `Content-Encoding: gzip`（SPEC.md §3.3）。
        """
        raw = self.client.raw(rel, refresh=refresh)
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw)

    def available_periods(self, market: str, namespace: str = "stock") -> list[str]:
        """快照里该市场该命名空间有哪些月份（已排序）。"""
        self._check_market(market)
        self._check_namespace(namespace)
        out = []
        for rel in self.client.files():
            m = _SHARD_RE.match(rel)
            if m and m.group("market") == market:
                is_idx = rel.startswith("daily/idx/")
                if is_idx == (namespace == "index"):
                    out.append(m.group("period"))
        return sorted(out)

    # ---------- 契约校验 ----------

    @staticmethod
    def _check_market(market: str) -> None:
        if market not in MARKETS:
            raise UnknownMarket(
                f"未知市场 {market!r}，合法值 {tuple(MARKETS)}")

    @staticmethod
    def _check_namespace(ns: str) -> None:
        if ns not in NAMESPACES:
            raise UnknownMarket(f"未知命名空间 {ns!r}，合法值 {NAMESPACES}")

    def _validate_header(self, header: dict, rel: str, market: str,
                         namespace: str, period: str) -> None:
        sch = header.get("schema")
        if not isinstance(sch, int) or sch > SUPPORTED_SCHEMA:
            raise ContractError(
                f"{rel}: schema={sch} 超出本客户端支持的 {SUPPORTED_SCHEMA} —— "
                f"请升级 ohlcvault（SPEC.md §11：字段语义变更必须拒绝）")
        for key, want in (("market", market), ("namespace", namespace),
                          ("period", period)):
            if header.get(key) != want:
                raise ContractError(
                    f"{rel}: 文件头 {key}={header.get(key)!r} 与路径不符（应为 {want!r}）")
        if header.get("adjust") != "none":
            raise ContractError(
                f"{rel}: adjust={header.get('adjust')!r}，契约要求恒为 'none'"
                f"（本数据集只存事实、不存视图，SPEC.md §9.2）")
        if header.get("volume_unit") != "share":
            raise ContractError(f"{rel}: volume_unit={header.get('volume_unit')!r}，应为 'share'")
        if header.get("af_basis") == "unavailable" and "af_scale" in header:
            raise ContractError(
                f"{rel}: af_basis='unavailable' 时不得出现 af_scale（SPEC.md §4.1）")

    def _validate_block(self, block: dict, header: dict, rel: str) -> None:
        """SPEC.md §4.2 的五条硬性约束。"""
        s = block.get("s")
        keys = ["d", "o", "h", "l", "c", "v", "a"]
        if "af" in block:
            keys.append("af")
        n = len(block.get("d") or [])
        for k in keys:
            arr = block.get(k)
            if arr is None:
                raise ContractError(f"{rel}:{s}: 缺少数组 {k!r}")
            if len(arr) != n:
                raise ContractError(
                    f"{rel}:{s}: {k} 长度 {len(arr)} ≠ d 长度 {n} —— 文件损坏")
        d = block["d"]
        if any(d[i] >= d[i + 1] for i in range(len(d) - 1)):
            raise ContractError(f"{rel}:{s}: d 非严格递增")
        period = header["period"]
        if d and (period_of(d[0]) != period or period_of(d[-1]) != period):
            raise ContractError(
                f"{rel}:{s}: d 落在 {period_of(d[0])}…{period_of(d[-1])}，"
                f"越出文件 period {period}")

    # ---------- 分片 ----------

    def shard(self, market: str, period: str, namespace: str = "stock",
              refresh: bool = False) -> dict:
        """取一个原始月分片（含文件头与全部 block）。带 LRU 缓存。"""
        self._check_market(market)
        self._check_namespace(namespace)
        rel = (f"daily/{'idx/' if namespace == 'index' else ''}"
               f"{market}/{period}.json.gz")
        if not refresh and rel in self._shard_cache:
            self._shard_cache.move_to_end(rel)
            return self._shard_cache[rel]
        doc = self._json(rel, refresh=refresh)
        if self.validate:
            self._validate_header(doc, rel, market, namespace, period)
            seen = set()
            for blk in doc["symbols"]:
                self._validate_block(blk, doc, rel)
                if blk["s"] in seen:
                    raise ContractError(f"{rel}: 重复的 s={blk['s']}")
                seen.add(blk["s"])
        self._shard_cache[rel] = doc
        while len(self._shard_cache) > self._shard_cache_max:
            self._shard_cache.popitem(last=False)
        return doc

    def _block(self, market: str, period: str, storage: str,
               namespace: str, refresh: bool) -> dict | None:
        doc = self.shard(market, period, namespace, refresh=refresh)
        for blk in doc["symbols"]:
            if blk["s"] == storage:
                return blk
        return None

    def _slice_block(self, blk: dict, start: int | None, end: int | None) -> dict | None:
        d = blk["d"]
        lo = 0 if start is None else bisect_left(d, start)
        hi = len(d) if end is None else bisect_right(d, end)
        if lo >= hi:
            return None
        out = {"s": blk["s"], **{k: blk[k][lo:hi] for k in ("d", "o", "h", "l", "c", "v", "a")},
               "af": blk["af"][lo:hi] if "af" in blk else None}
        return out

    # ---------- meta ----------

    def _meta(self, rel: str) -> dict:
        if rel not in self._meta_cache:
            self._meta_cache[rel] = self._json(rel)
        return self._meta_cache[rel]

    def symbols(self, market: str, type: str | None = None,   # noqa: A002
                status: str | None = None, board: str | None = None,
                include_indices: bool = True) -> list[dict]:
        """标的清单。默认 `include_indices=False` 时只返回 type=stock/etf。

        退市股**在池内**（`status="delisted"`），这是本数据集相对爬虫类数据源的
        结构性优势 —— 回测池若只含今天还活着的股票，历史收益会被系统性高估。
        """
        self._check_market(market)
        doc = self._meta(f"meta/symbols/{market}.json")
        out = []
        for e in doc["symbols"]:
            if type is not None and e["type"] != type:
                continue
            if not include_indices and type is None and e["type"] == "index":
                continue
            if status is not None and e["status"] != status:
                continue
            if board is not None and e.get("board") != board:
                continue
            out.append(e)
        return out

    def symbol(self, code: str, market: str | None = None) -> dict:
        """查单个标的的清单条目（含名称/上市日/退市状态/板块）。"""
        storage = normalize(code, market=market)
        mkt = market or market_of(storage)
        doc = self._meta(f"meta/symbols/{mkt}.json")
        for e in doc["symbols"]:
            if e["s"] == storage:
                return e
        raise KeyError(f"{to_api(storage)} 不在 meta/symbols/{mkt}.json 中")

    def symbol_names(self, market: str) -> dict[str, str]:
        """API 式代码 → 名称。给报告/日志用，避免打印裸代码。"""
        return {e["api"]: e["name"] for e in self.symbols(market)}

    def coverage_note(self, market: str) -> str:
        """覆盖缺口的自然语言说明（供日志/报告直接引用）。"""
        return self.coverage(market).get("note", "")

    def calendar(self, market: str, start: int | None = None,
                 end: int | None = None) -> list[int]:
        """交易日列表（升序）。

        `days` 里可能含未来日期（A 股来自交易所排班表），**但不得依赖它** ——
        权威区间只到「最后一个 ≤ 今天的交易日」（SPEC.md §6.2）。
        """
        start = _as_ymd(start, "start")
        end = _as_ymd(end, "end")
        self._check_market(market)
        doc = self._meta(f"meta/calendar/{market}.json")
        days = doc["days"]
        lo = 0 if start is None else bisect_left(days, start)
        hi = len(days) if end is None else bisect_right(days, end)
        return days[lo:hi]

    # ---------- 时间序列 ----------

    def daily(self, code: str, start: int | None = None, end: int | None = None,
              market: str | None = None, refresh: bool = False) -> Bars:
        """股票日线（跨月自动拼接）。"""
        return self._series(code, start, end, market, "stock", refresh)

    def index_daily(self, code: str, start: int | None = None,
                    end: int | None = None, market: str | None = None,
                    refresh: bool = False) -> Bars:
        """指数日线。指数在**独立命名空间** `daily/idx/` 下，不与个股混装
        （两者的 `volume_unit` 归一乘数不同，混在一个文件里会污染全局口径）。"""
        return self._series(code, start, end, market, "index", refresh)

    def _series(self, code: str, start: int | str | None, end: int | str | None,
                market: str | None, namespace: str, refresh: bool) -> Bars:
        start = _as_ymd(start, "start")
        end = _as_ymd(end, "end")
        storage = normalize(code, market=market)
        mkt = market or market_of(storage)
        self._check_market(mkt)
        return self._assemble(storage, mkt, namespace, start, end, refresh)

    def _assemble(self, storage: str, mkt: str, namespace: str,
                  start: int | None, end: int | None, refresh: bool) -> Bars:
        periods = self.available_periods(mkt, namespace)
        if start is not None or end is not None:
            lo = start if start is not None else 0
            hi = end if end is not None else 99991231
            keep = set(periods_between(lo, hi))
            periods = [p for p in periods if p in keep]

        acc: dict[str, list] = {k: [] for k in
                                ("d", "o", "h", "l", "c", "v", "a", "af")}
        header: dict | None = None
        has_af = True
        for p in periods:
            blk = self._block(mkt, p, storage, namespace, refresh)
            if blk is None:
                continue                      # 该月无数据（未上市/停牌/退市后）
            if header is None:
                header = self.shard(mkt, p, namespace, refresh=refresh)
            chunk = self._slice_block(blk, start, end)
            if chunk is None:
                continue
            for k in ("d", "o", "h", "l", "c", "v", "a"):
                acc[k].extend(chunk[k])
            if chunk["af"] is None:
                has_af = False
            else:
                acc["af"].extend(chunk["af"])

        if header is None:
            # 快照里完全没有该标的任何月份 —— 先确认它确实在清单里，
            # 否则把「代码打错了」伪装成「没有数据」是很坏的体验
            try:
                self.symbol(storage, market=mkt)
            except KeyError:
                raise KeyError(
                    f"{to_api(storage)} 既不在快照数据中，也不在 "
                    f"meta/symbols/{mkt}.json 中 —— 检查代码是否写错") from None
            header = {"price_scale": PRICE_SCALE, "currency": MARKETS[mkt]["currency"],
                      "volume_unit": "share", "af_basis": "unavailable",
                      "source": "", "af_scale": None}

        if self.validate:
            self._validate_series(acc, start, end, storage)

        return Bars(
            symbol=to_api(storage), market=mkt, namespace=namespace,
            price_scale=header.get("price_scale", PRICE_SCALE),
            currency=header.get("currency", MARKETS[mkt]["currency"]),
            volume_unit=header.get("volume_unit", "share"),
            af_basis=header.get("af_basis", "unavailable"),
            af_scale=header.get("af_scale"),
            source=header.get("source", ""),
            d=acc["d"], o=acc["o"], h=acc["h"], l=acc["l"], c=acc["c"],
            v=acc["v"], a=acc["a"], af=acc["af"] if has_af else None,
        )

    @staticmethod
    def _validate_series(acc: dict, start: int | None, end: int | None,
                         storage: str) -> None:
        d = acc["d"]
        if any(d[i] >= d[i + 1] for i in range(len(d) - 1)):
            raise ContractError(
                f"{storage}: 跨月拼接后 d 非严格递增（分片可能重叠或串月）")
        if start is not None and d and d[0] < start:
            raise ContractError(f"{storage}: 结果含早于 start 的行")
        if end is not None and d and d[-1] > end:
            raise ContractError(f"{storage}: 结果含晚于 end 的行")

    def daily_many(self, codes: Iterable[str], start: int | None = None,
                   end: int | None = None, market: str | None = None,
                   namespace: str = "stock",
                   refresh: bool = False) -> dict[str, Bars]:
        """批量取多个标的时间序列 —— **回测请优先用这个**。

        分片形态下，同一月份的 N 个标的本就同处一个文件。逐个调 `daily()`
        会对同一个月文件解压 N 次；这里按「月 → 该月所有目标标的」组织，
        每个分片只装载一次。
        """
        storages: dict[str, tuple[str, str]] = {}
        for c in codes:
            st = normalize(c, market=market)
            mkt = market or market_of(st)
            self._check_market(mkt)
            storages[st] = (mkt, to_api(st))
        if not storages:
            return {}
        start = _as_ymd(start, "start")
        end = _as_ymd(end, "end")

        # 按 (市场, 月份) 归组
        by_period: dict[tuple[str, str], list[str]] = {}
        for st, (mkt, _) in storages.items():
            periods = self.available_periods(mkt, namespace)
            if start is not None or end is not None:
                keep = set(periods_between(start if start is not None else 0,
                                           end if end is not None else 99991231))
                periods = [p for p in periods if p in keep]
            for p in periods:
                by_period.setdefault((mkt, p), []).append(st)

        acc: dict[str, dict] = {st: {"d": [], "o": [], "h": [], "l": [], "c": [],
                                     "v": [], "a": [], "af": []}
                                for st in storages}
        meta: dict[str, dict] = {}
        has_af = dict.fromkeys(storages, False)

        for (mkt, p), wanted in sorted(by_period.items()):
            doc = self.shard(mkt, p, namespace, refresh=refresh)
            want_set = set(wanted)
            for blk in doc["symbols"]:
                st = blk["s"]
                if st not in want_set:
                    continue
                if st not in meta:
                    meta[st] = doc
                chunk = self._slice_block(blk, start, end)
                if chunk is None:
                    continue
                tgt = acc[st]
                for k in ("d", "o", "h", "l", "c", "v", "a"):
                    tgt[k].extend(chunk[k])
                if chunk["af"] is not None:
                    has_af[st] = True
                    tgt["af"].extend(chunk["af"])

        out: dict[str, Bars] = {}
        for st, (mkt, api) in storages.items():
            h = meta.get(st) or {"price_scale": PRICE_SCALE,
                                 "currency": MARKETS[mkt]["currency"],
                                 "volume_unit": "share",
                                 "af_basis": "unavailable", "source": "",
                                 "af_scale": None}
            if self.validate:
                self._validate_series(acc[st], start, end, st)
            out[api] = Bars(
                symbol=api, market=mkt, namespace=namespace,
                price_scale=h.get("price_scale", PRICE_SCALE),
                currency=h.get("currency", MARKETS[mkt]["currency"]),
                volume_unit=h.get("volume_unit", "share"),
                af_basis=h.get("af_basis", "unavailable"),
                af_scale=h.get("af_scale"), source=h.get("source", ""),
                d=acc[st]["d"], o=acc[st]["o"], h=acc[st]["h"], l=acc[st]["l"],
                c=acc[st]["c"], v=acc[st]["v"], a=acc[st]["a"],
                af=acc[st]["af"] if has_af[st] else None,
            )
        return out

    # ---------- 断面 ----------

    def cross_section(self, market: str, date: int | str,
                      sort_by: str = "amount", limit: int | None = None,
                      ascending: bool = False,
                      namespace: str = "stock") -> list[dict]:
        """某一交易日的全市场截面，默认按成交额降序。

        **不新增任何数据文件**：月分片本身就装着整月全部标的，读一个文件转置即可。

        `sort_by="pct_chg"` 的涨幅只用**同一分片内**的前一行作基准，
        因此该月首个交易日给不出涨幅（返回 `None`）—— 这是刻意的，
        跨月取上一行会让「读一个文件」退化成「读两个文件」。
        """
        self._check_market(market)
        self._check_namespace(namespace)
        if sort_by not in _SORTABLE:
            raise ValueError(
                f"sort_by={sort_by!r} 不支持，合法值 {list(_SORTABLE)}")
        date = _as_ymd(date, "date")
        if date is None:
            raise DateError(
                'cross_section 需要一个具体交易日，如 20260918 或 "2026-09-18"')
        period = period_of(date)
        doc = self.shard(market, period, namespace)
        ps = doc.get("price_scale", PRICE_SCALE)

        rows = []
        for blk in doc["symbols"]:
            d = blk["d"]
            i = bisect_left(d, date)
            if i >= len(d) or d[i] != date:
                continue
            row = {
                "symbol": to_api(blk["s"]),
                "storage": blk["s"],
                "d": date,
                "open": blk["o"][i] / ps,
                "high": blk["h"][i] / ps,
                "low": blk["l"][i] / ps,
                "close": blk["c"][i] / ps,
                "volume": blk["v"][i],
                "amount": blk["a"][i],
            }
            if "af" in blk and blk["af"]:
                afs = doc.get("af_scale") or AF_SCALE
                row["af"] = blk["af"][i] / afs
            if sort_by == "pct_chg":
                prev = blk["c"][i - 1] if i > 0 else None
                row["pct_chg"] = (None if not prev
                                  else (blk["c"][i] - prev) / prev * 100.0)
            rows.append(row)

        rows = _sort_none_last(rows, sort_by, ascending)
        if limit is not None:
            rows = rows[:limit]
        return rows

    def cross_section_pandas(self, market: str, date: int, **kw):
        """`cross_section()` 的 DataFrame 版本（需要 pandas）。"""
        try:
            import pandas as pd
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "cross_section_pandas() 需要 pandas：pip install ohlcvault[pandas]") from e
        return pd.DataFrame(self.cross_section(market, date, **kw))
