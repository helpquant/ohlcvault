"""传输层：多镜像读取 + sha256 校验 + 内容寻址本地缓存。

这是「数据一定能用」的全部实现：

1. 任意一个镜像可达 → 数据拿得到
2. sha256 与快照清单不符 → 该镜像的这份数据被丢弃并降级到下一个
   （**绝不把脏数据交给调用方** —— 这是校验存在的唯一理由）
3. 全部镜像不可达 → 从本地缓存取（已拉过的数据永久可用，可离线复现）
4. 缓存有效性由清单里的 sha256 决定，**不靠时间戳** —— 内容寻址的威力所在

两种模式：

- floating（默认）读 `latest.json`，跟随最新快照
- pinned 指定快照 id，冻结历史状态，用于复现研究

镜像可以是 http(s) URL，也可以是本地目录（便于离线测试）。
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from .errors import HashMismatch, SnapshotUnavailable  # noqa: F401  （对外保留此名）
from .net import HTTPPool

CHUNK = 1 << 20


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class MarketClient:
    """读一个内容寻址的快照数据服务。"""

    def __init__(
        self,
        mirrors: list[str],
        cache_dir: str | Path,
        snapshot: str | None = None,
        timeout: int = 15,
    ):
        if not mirrors:
            raise ValueError("至少需要一个镜像")
        self.mirrors = [str(m).rstrip("/") for m in mirrors]
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.pinned = snapshot
        self.timeout = timeout
        self.trace: list[str] = []
        self.stats: dict = {
            "net": 0, "cache_hit": 0, "hash_reject": 0, "mirror_fail": 0,
            "served_by": {}, "offline": False,
        }
        self._mf: dict | None = None
        self._sid: str | None = None
        self._lock = threading.Lock()
        self._pool = HTTPPool(timeout=timeout)

    # ---------- 底层 IO ----------

    @staticmethod
    def _label(m: str) -> str:
        return m.rsplit("/", 2)[-2] if m.startswith("http") else Path(m).name

    def _fetch_raw(self, m: str, rel: str) -> bytes:
        if m.startswith("http"):
            # keep-alive 连接池：分片是几百连发的场景，复用 TLS 连接
            # 把冷缓存全历史拉取从「分钟级握手开销」里解放出来（net.py）。
            # 自定义 UA 仍然必须：Cloudflare 边缘默认拦截 Python-urllib/*（403）。
            self.stats["net"] += 1
            body, status = self._pool.get(f"{m}/{rel}")
            if status >= 400:
                raise OSError(f"HTTP {status}: {m}/{rel}")
            return body
        p = Path(m) / rel
        if not p.is_file():
            raise FileNotFoundError(rel)
        return p.read_bytes()

    def _promote(self, m: str) -> None:
        """故障转移记忆：成功过的镜像提到首选位，后续请求不再重复探测死镜像。"""
        if self.mirrors and self.mirrors[0] != m:
            self.mirrors.remove(m)
            self.mirrors.insert(0, m)
            self.trace.append(f"    亲和切换 → {self._label(m)} 提为首选镜像")

    def _read(self, rel: str) -> bytes:
        """按镜像顺序取一个相对路径，任一成功即返回。"""
        for m in list(self.mirrors):
            label = self._label(m)
            try:
                body = self._fetch_raw(m, rel)
            except Exception as e:
                self.stats["mirror_fail"] += 1
                self.trace.append(f"    镜像 {label} 不可达（{type(e).__name__}）")
                continue
            self.stats["served_by"][label] = self.stats["served_by"].get(label, 0) + 1
            self.trace.append(f"    镜像 {label} 命中 → {rel}")
            self._promote(m)
            return body
        raise SnapshotUnavailable(f"全部镜像均不可达: {rel}")

    def _manifest_cache_file(self) -> Path:
        return self.cache / "_manifest.json"

    # ---------- 快照解析 ----------

    def manifest(self, refresh: bool = False) -> dict:
        with self._lock:
            if self._mf is not None and not refresh:
                return self._mf

            want = self.pinned
            rel = f"snapshots/{want}/manifest.json" if want else "latest.json"
            try:
                idx = json.loads(self._read(rel))
                sid = idx["snapshot"]
                if rel != f"snapshots/{sid}/manifest.json":
                    mf = json.loads(self._read(f"snapshots/{sid}/manifest.json"))
                else:
                    mf = idx
                self._mf, self._sid = mf, sid
                self._manifest_cache_file().write_bytes(json.dumps(mf).encode())
                return mf
            except SnapshotUnavailable:
                cached = self._manifest_cache_file()
                if cached.is_file():
                    mf = json.loads(cached.read_bytes())
                    self._mf, self._sid = mf, mf["snapshot"]
                    self.stats["offline"] = True
                    self.trace.append(
                        f"    全部镜像失败 → 使用本地缓存清单（快照 {self._sid}）")
                    return mf
                raise SnapshotUnavailable(
                    "无镜像可达，且本地无缓存清单 —— 首次使用需联网") from None

    @property
    def snapshot(self) -> str:
        if self._mf is None:
            self.manifest()
        return self._sid  # type: ignore[return-value]

    def files(self) -> list[str]:
        """本快照包含的全部相对路径（排序后）。"""
        return sorted(self.manifest()["files"])

    # ---------- 取数 ----------

    def raw(self, rel: str, refresh: bool = False) -> bytes:
        """取一个文件的**原始字节**（已通过 sha256 校验）。

        调用方若要自己解压/解析就用这个 —— 校验的对象始终是传输中的字节，
        不是解压后的明文。这个区别很重要：若分发端错误地设了
        `Content-Encoding: gzip`，客户端拿到的就是被自动解压过的明文，
        哈希必然对不上，表现为「数据被篡改」的假警报（SPEC.md §3.3）。
        """
        mf = self.manifest(refresh=refresh)
        meta = mf["files"].get(rel)
        if meta is None:
            raise KeyError(f"快照中不存在: {rel}")
        want_hash = meta["sha256"]
        cache_file = self.cache / self.snapshot / rel

        # 1) 本地缓存 —— 有效性由 hash 决定，与时间无关
        if cache_file.is_file() and sha256_file(cache_file) == want_hash:
            self.stats["cache_hit"] += 1
            self.trace.append(f"    本地缓存命中（hash 一致）→ {rel}")
            return cache_file.read_bytes()

        # 2) 逐镜像请求 + 逐镜像校验
        for m in list(self.mirrors):
            label = self._label(m)
            try:
                body = self._fetch_raw(m, rel)
            except Exception as e:
                self.stats["mirror_fail"] += 1
                self.trace.append(f"    镜像 {label} 不可达（{type(e).__name__}）")
                continue

            if sha256_bytes(body) != want_hash:
                self.stats["hash_reject"] += 1
                self.trace.append(f"    镜像 {label} 内容与清单不符 → 拒绝，继续降级")
                continue

            self.stats["served_by"][label] = self.stats["served_by"].get(label, 0) + 1
            self.trace.append(f"    镜像 {label} 取回并通过校验 → {rel}")
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(body)
            self._promote(m)
            return body

        raise SnapshotUnavailable(
            f"{rel}: 无可信来源（镜像全部不可达或校验失败，"
            f"已拒绝 {self.stats['hash_reject']} 份脏数据）")

    def get(self, rel: str, refresh: bool = False) -> dict:
        """取一个明文 JSON 文件。"""
        return json.loads(self.raw(rel, refresh=refresh))
