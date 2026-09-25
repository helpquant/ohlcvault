"""传输优化：按 host 复用 TCP/TLS 连接（HTTP keep-alive）。

为什么存在这个模块：月分片按月×市场组织，拉一只股票的全历史 =
连续请求几百个分片。urllib 每次请求都做一轮完整的 TCP + TLS 握手
（实测约 2.3s/片），冷缓存拉全历史要十几分钟 —— 其中 >95% 是握手。

这里用标准库 `http.client` 做一个极小的按 host 连接池：

- 同一 host 的请求复用已有连接（TLS 会话也不必重建）
- 服务端提前关闭的陈旧连接 → 自动重建重试一次
- 显式 `Accept-Encoding: identity`：**绝不让边缘节点自动压缩** ——
  本客户端校验的是传输字节（SPEC.md §3.3），自动解压会让哈希必然失配
- 遵循 301/302/303/307/308 重定向（urllib 自动做的事，这里手工做）

仍然零运行时依赖：只用标准库。线程安全（每 host 一把锁）。
"""
from __future__ import annotations

import http.client
import threading
from urllib.parse import urljoin, urlsplit

from .config import USER_AGENT

_MAX_REDIRECTS = 5
_REDIRECT = {301, 302, 303, 307, 308}


class HTTPPool:
    """极小的 keep-alive 连接池：一个 host 一条活跃连接、一把锁。"""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._conns: dict[tuple[str, str], http.client.HTTPConnection] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()
        self.stats = {"requests": 0, "reused": 0, "fresh": 0, "retries": 0}

    # ---------- 对外 ----------

    def get(self, url: str) -> tuple[bytes, int]:
        """GET 一个 URL，返回 `(body, status)`。

        重定向在内部跟随（最多 `_MAX_REDIRECTS` 次）；
        4xx/5xx 不抛异常、原样返回状态码，由调用方决定语义。
        """
        hdrs = {
            "User-Agent": USER_AGENT,
            # 关键：禁止边缘自动压缩。校验对象是传输字节（SPEC.md §3.3），
            # Content-Encoding: gzip 会让 sha256 必然失配。
            "Accept-Encoding": "identity",
        }
        for _ in range(_MAX_REDIRECTS + 1):
            sp = urlsplit(url)
            key = (sp.scheme, sp.netloc)
            with self._host_lock(key):
                body, status, loc = self._once(key, sp, hdrs)
            if status in _REDIRECT and loc:
                url = urljoin(url, loc)
                continue
            return body, status
        raise OSError(f"重定向超过 {_MAX_REDIRECTS} 次: {url}")

    def close(self) -> None:
        with self._guard:
            for c in self._conns.values():
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()

    # ---------- 内部 ----------

    def _host_lock(self, key) -> threading.Lock:
        with self._guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def _once(self, key, sp, hdrs) -> tuple[bytes, int, str | None]:
        """单次请求；陈旧连接（服务端已关闭）自动重建重试一次。"""
        for attempt in (0, 1):
            try:
                conn = self._conns.get(key)
                if conn is None:
                    conn = self._new(key)
                    self.stats["fresh"] += 1
                else:
                    self.stats["reused"] += 1
                conn.request("GET", self._path(sp), headers=hdrs)
                r = conn.getresponse()
                body = r.read()
                self.stats["requests"] += 1
                return body, r.status, r.getheader("Location")
            except Exception:
                old = self._conns.pop(key, None)
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
                self.stats["retries"] += 1
                if attempt == 1:
                    raise
        raise OSError("unreachable")  # pragma: no cover

    def _new(self, key) -> http.client.HTTPConnection:
        scheme, netloc = key
        cls = (http.client.HTTPSConnection if scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(netloc, timeout=self.timeout)
        self._conns[key] = conn
        return conn

    @staticmethod
    def _path(sp) -> str:
        p = sp.path or "/"
        return f"{p}?{sp.query}" if sp.query else p
