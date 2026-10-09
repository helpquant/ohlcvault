"""测试夹具。

两类测试：

- **纯单元测试**（符号规范、复权整数运算、契约校验）—— 无需任何数据，随时可跑。
- **真实分片测试** —— 需要一个符合契约的数据根（含 `latest.json` 与
  `snapshots/{sid}/manifest.json`）。默认找兄弟目录 `../ohlcvault_server/data`，
  也可用环境变量 `OHLCVAULT_DATA` 指定。找不到就 `skip`，**不造假数据**。

为什么不用合成数据兜底：合成数据只能验证「代码能跑」，验证不了「契约没歪」。
本项目的整个立论就是与契约一致，用假数据测等于绕过唯一有价值的断言。
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DEFAULT_DATA = ROOT.parent / "ohlcvault_server" / "data"


def data_root() -> Path | None:
    cand = Path(os.environ.get("OHLCVAULT_DATA") or _DEFAULT_DATA)
    if (cand / "latest.json").is_file() and (cand / "snapshots").is_dir():
        return cand
    return None


@pytest.fixture(scope="session")
def data_dir() -> Path:
    d = data_root()
    if d is None:
        pytest.skip(
            "未找到数据根（需要含 latest.json 与 snapshots/ 的目录）。"
            "设置 OHLCVAULT_DATA 或把 ohlcvault_server/data 放在兄弟目录。")
    return d


@pytest.fixture()
def store(data_dir, tmp_path):
    """针对真实分片的 Store（缓存放在 tmp，避免污染开发者缓存）。"""
    import ohlcvault as ov
    return ov.connect(mirrors=[str(data_dir)], cache_dir=tmp_path / "cache", quiet=True)


def _copy_tree_cow(src: Path, dst: Path) -> None:
    """复制目录树，尽可能用「写时复制」（APFS clonefile / reflink）。

    mirrors 夹具要为**每个**测试准备多份数据副本；真实数据集在 GB 量级，
    逐字节复制三遍就是数个 GB —— 会直接吃光开发机磁盘（实测测试进程被
    SIGKILL 打断，就是因为这里）。COW 副本几乎不额外占空间，而且**写入时
    自动分离**，所以「篡改某个副本」的测试语义完全成立（硬链接做不到：
    原地改写会串到其他副本）。平台不支持时回退到普通复制。
    """
    import subprocess

    dst.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        cmd = ["cp", "-Rc"]
    elif sys.platform.startswith("linux"):
        cmd = ["cp", "-a", "--reflink=auto"]
    else:
        cmd = []
    if cmd:
        r = subprocess.run(cmd + [str(src), str(dst)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            return
    shutil.copytree(src, dst, dirs_exist_ok=True)


@pytest.fixture()
def mirrors(data_dir, tmp_path):
    """三份独立副本，模拟「主源 + 两个备用源」的镜像链。

    这里的三份是**占位符**（名字沿用历史，不代表当前真实镜像链 —— 真实链见
    `ohlcvault.config.DEFAULT_MIRRORS`，2026-10-09 起只有两个源）。保留三份是为了
    让「N 路降级」这条逻辑本身被测到，而不是绑死在当前有几个源上。

    用真实字节而不是合成数据 —— 校验、降级、篡改拒绝这些路径只有在
    真实的 gzip 分片上跑才有意义。
    """
    base = tmp_path / "remote"
    names = ["cloudflare_r2", "jsdelivr", "github_raw"]
    for n in names:
        _copy_tree_cow(data_dir, base / n)
        for skip in (".cache", ".months"):
            shutil.rmtree(base / n / skip, ignore_errors=True)
    return [str(base / n) for n in names], base


def manifest_of(data_dir: Path) -> dict:
    sid = json.loads((data_dir / "latest.json").read_bytes())["snapshot"]
    return json.loads((data_dir / "snapshots" / sid / "manifest.json").read_bytes())


def first_shard(data_dir: Path, ns: str = "stock") -> str:
    """挑一个存在的月分片相对路径，供篡改类测试使用。"""
    for rel in sorted(manifest_of(data_dir)["files"]):
        if rel.startswith("daily/") and rel.endswith(".json.gz"):
            if (ns == "index") == rel.startswith("daily/idx/"):
                return rel
    raise AssertionError("快照里没有可用的月分片")


def read_json_gz(path: Path):
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)
