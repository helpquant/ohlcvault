"""默认配置：镜像链、缓存位置、契约常量。

镜像链的依据是「**故障域不重叠**」——三个源的失败原因必须互不相同，否则
只是同一个故障的三倍冗余（DESIGN.md §9.2）：

| 镜像 | 角色 | 故障域 |
|---|---|---|
| Cloudflare R2 + CDN | 主源 | 商业 CDN：账号封禁、账单异常、区域故障 |
| GitHub raw / jsDelivr | 备用 1 | 微软系托管：与 Cloudflare 完全独立 |
| 自有服务器 HTTPS | 备用 2 | 自有机器：唯一完全可控的兜底 |

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

NAMESPACES = ("stock", "index")

# 默认镜像链。可用 OHLCVAULT_MIRRORS 覆盖。
DEFAULT_MIRRORS: list[str] = [
    "https://data.ohlcvault.com",
    "https://cdn.jsdelivr.net/gh/helpquant/ohlcvault-data@main",
    "https://raw.githubusercontent.com/helpquant/ohlcvault-data/main",
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
