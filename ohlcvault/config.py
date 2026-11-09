"""默认配置：镜像链、缓存位置、契约常量。

镜像链的依据是「**故障域不重叠**」——三个源的失败原因必须互不相同，否则
只是同一个故障的三倍冗余（DESIGN.md §9.2）：

| 镜像 | 角色 | 故障域 |
|---|---|---|
| `data.ohlcvault.com`（Cloudflare R2 + CDN） | 主源 | 商业 CDN：账号封禁、账单异常、区域故障 |
| `data.helpquant.com`（自有服务器 HTTPS） | 备用 1 | 自有机器 + 自有 DNS（DNSPod）：唯一完全可控的兜底 |

历史上这里还有第三个源（`cdn.jsdelivr.net/gh/…` 与 `raw.githubusercontent.com/…`），
指向数据镜像仓库 `helpquant/ohlcvault-data`。该仓库经 GitHub API 查证 **404，从未
创建**，两个 URL 都是死链，只会让客户端在冷启动时白付一次超时 —— 已移除。
日后若真建了 GitHub 镜像仓库，再把它们加回来即可（届时故障域才是真正独立的）。

客户端会**记住哪个镜像成功过**并把它提到首位，所以镜像顺序不是性能关键，
减少的是「每次请求都先付一次失败超时」。
"""
from __future__ import annotations

import os
from pathlib import Path

# 契约常量（SPEC.md §4.1 / §9.1）。注意这些值也出现在每个文件的**文件头**里，
# 客户端一律读文件头而不硬编码 —— 这里只是当前版本的默认值。
PRICE_SCALE = 1000
AF_SCALE = 1_000_000
SUPPORTED_SCHEMA = 1

MARKETS: dict[str, dict] = {
    "cn": {"name": "A股", "timezone": "Asia/Shanghai", "currency": "CNY"},
    "hk": {"name": "港股", "timezone": "Asia/Hong_Kong", "currency": "HKD"},
    "us": {"name": "美股", "timezone": "America/New_York", "currency": "USD"},
}

NAMESPACES = ("stock", "index", "min15")

# HTTP User-Agent。必须带自定义 UA：Cloudflare 等边缘节点默认拦截
# Python-urllib/* 的请求（403），裸 urllib 会被当成 bot。
VERSION = "0.3.1"
USER_AGENT = f"ohlcvault/{VERSION} (+https://github.com/helpquant/ohlcvault)"

# 默认镜像链。可用 OHLCVAULT_MIRRORS 覆盖。
DEFAULT_MIRRORS: list[str] = [
    "https://data.ohlcvault.com",
    "https://data.helpquant.com",
]

DEFAULT_CACHE = Path(
    os.environ.get("OHLCVAULT_CACHE")
    or (Path.home() / ".cache" / "ohlcvault")
)

TIMEOUT = int(os.environ.get("OHLCVAULT_TIMEOUT") or 15)


def mirrors_from_env() -> list[str]:
    """`OHLCVAULT_MIRRORS=a,b,c` 覆盖默认镜像链（本地目录也可）。"""
    raw = os.environ.get("OHLCVAULT_MIRRORS", "").strip()
    if not raw:
        return list(DEFAULT_MIRRORS)
    return [m.strip() for m in raw.split(",") if m.strip()]
