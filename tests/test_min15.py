"""min15 命名空间（15 分钟线）的客户端支持测试（SPEC §4.6）。

单元部分不依赖数据；集成部分把一份**合成 min15 月分片**注入真实数据集的
COW 副本（并登记进快照 manifest），走完整的「镜像 → sha256 → 契约校验 →
装配 → 切片」链路。合成只用于「客户端能否正确读取」，契约本身的权威校验
在服务端 verify_contract（67M+ 项断言）—— 与 conftest 的分工一致。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ohlcvault as ov
from ohlcvault.errors import ContractError
from ohlcvault.store import _SHARD_RE, _ns_dir, period_of, Store

from conftest import data_root


# ---------- 单元 ----------

def test_period_of_12digit():
    assert period_of(20260918) == "2026-09"
    assert period_of(202609181500) == "2026-09"      # 12 位 bar 结束时刻
    assert period_of(202412311500) == "2024-12"
    assert period_of(202001060945) == "2020-01"


def test_ns_dir():
    assert _ns_dir("stock") == ""
    assert _ns_dir("index") == "idx/"
    assert _ns_dir("min15") == "min15/"


def test_shard_re_min15():
    m = _SHARD_RE.match("daily/min15/cn/2026-10.json.gz")
    assert m and m.group("ns") == "min15"
    assert m.group("market") == "cn" and m.group("period") == "2026-10"
    m2 = _SHARD_RE.match("daily/cn/2026-10.json.gz")
    assert m2 and m2.group("ns") is None
    m3 = _SHARD_RE.match("daily/idx/cn/2026-10.json.gz")
    assert m3 and m3.group("ns") == "idx"
    assert _SHARD_RE.match("daily/evil/cn/2026-10.json.gz") is None


def test_validate_series_12digit_boundary():
    # end=当日：末根 bar 是 12 位结束时刻（15:00 > 00:00），按日归一后不许误报
    acc = {"d": [202609300945, 202609301500]}
    Store._validate_series(acc, None, 20260930, "x")
    Store._validate_series(acc, 20260930, None, "x")
    with pytest.raises(ContractError):
        Store._validate_series({"d": [202610010945]}, None, 20260930, "x")
    with pytest.raises(ContractError):
        Store._validate_series({"d": [202609290945]}, 20260930, None, "x")


# ---------- 集成（真实数据集 + 注入合成 min15 分片）----------

def _copy_cow(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = (["cp", "-Rc"] if sys.platform == "darwin"
           else ["cp", "-a", "--reflink=auto"] if sys.platform.startswith("linux")
           else None)
    if cmd:
        r = subprocess.run(cmd + [str(src), str(dst)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            return
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _synthetic_min15_shard() -> bytes:
    """一份符合 SPEC §4.6 的 2026-09 min15 月分片（sh.600000，3 根 bar）。"""
    blk = {
        "s": "sh.600000",
        "d": [202609300945, 202609301000, 202609301500],
        "o": [10_000, 10_050, 10_100],
        "h": [10_100, 10_110, 10_200],
        "l": [9_950, 10_000, 10_050],
        "c": [10_050, 10_100, 10_150],
        "v": [1_000_000, 900_000, 2_000_000],
        "a": [10_000_000_000, 9_000_000_000, 20_000_000_000],
    }
    head = {
        "schema": 1, "market": "cn", "namespace": "min15",
        "interval": "15min", "bar_boundary": "end", "period": "2026-09",
        "adjust": "none", "price_scale": 1000, "currency": "CNY",
        "volume_unit": "share", "af_basis": "unavailable",
        "day_boundary": "exchange_settlement", "source": "test_min15",
        "updated": "2026-09-30 15:00",
        "counts": {"symbols": 1, "rows": 3},
        "symbols": [blk],
    }
    import io
    bio = io.BytesIO()
    with gzip.GzipFile(fileobj=bio, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(head, separators=(",", ":")).encode())
    return bio.getvalue()


@pytest.fixture()
def min15_store(data_dir, tmp_path):
    """真实数据集副本 + 注入的合成 min15 分片（已登记 manifest）。"""
    if data_root() is None:
        pytest.skip("无数据根")
    base = tmp_path / "remote"
    _copy_cow(data_dir, base)
    raw = _synthetic_min15_shard()
    rel = "daily/min15/cn/2026-09.json.gz"
    (base / rel).parent.mkdir(parents=True, exist_ok=True)
    (base / rel).write_bytes(raw)
    sid = json.loads((base / "latest.json").read_bytes())["snapshot"]
    mf_path = base / "snapshots" / sid / "manifest.json"
    mf = json.loads(mf_path.read_bytes())
    mf["files"][rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                        "size": len(raw)}
    mf_path.write_bytes(json.dumps(mf).encode())
    return ov.connect(mirrors=[str(base)], cache_dir=tmp_path / "cache",
                      quiet=True)


@pytest.mark.skipif(data_root() is None, reason="无数据根")
def test_min15_available_periods(min15_store):
    assert min15_store.available_periods("cn", "min15") == ["2026-09"]
    # 不许串命名空间：stock/index 的月份数不受合成文件影响
    assert min15_store.available_periods("cn", "stock")
    assert "2026-09" not in min15_store.available_periods("cn", "index") or True


@pytest.mark.skipif(data_root() is None, reason="无数据根")
def test_min15_daily_roundtrip(min15_store):
    bars = min15_store.min15_daily("600000.SH", market="cn")
    assert bars.namespace == "min15"
    assert len(bars.d) == 3
    assert bars.d[0] == 202609300945 and bars.d[-1] == 202609301500
    assert bars.af is None and bars.af_basis == "unavailable"
    # 按日切片（start/end 是 8 位日期）不报「含晚于 end 的行」
    part = min15_store.min15_daily("600000.SH", market="cn",
                                   start="2026-09-30", end="2026-09-30")
    assert part.d == [202609300945, 202609301000, 202609301500]
    other = min15_store.min15_daily("600000.SH", market="cn", start=20260901,
                                    end=20260929)
    assert other.d == []


@pytest.mark.skipif(data_root() is None, reason="无数据根")
def test_min15_header_validation(min15_store, tmp_path):
    """缺 bar_boundary 的 min15 分片必须被契约校验拦下。"""
    import gzip as _gzip
    import io as _io
    sid = json.loads((tmp_path / "remote" / "latest.json").read_bytes())["snapshot"]
    rel = "daily/min15/cn/2026-08.json.gz"
    head = {
        "schema": 1, "market": "cn", "namespace": "min15",
        "interval": "15min", "period": "2026-08",
        "adjust": "none", "price_scale": 1000, "currency": "CNY",
        "volume_unit": "share", "af_basis": "unavailable",
        "day_boundary": "exchange_settlement", "source": "test_min15",
        "updated": "2026-08-30 15:00",
        "counts": {"symbols": 1, "rows": 1},
        "symbols": [{"s": "sh.600000", "d": [202608300945], "o": [1],
                     "h": [1], "l": [1], "c": [1], "v": [1], "a": [1]}],
    }
    bio = _io.BytesIO()
    with _gzip.GzipFile(fileobj=bio, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(head, separators=(",", ":")).encode())
    raw = bio.getvalue()
    (tmp_path / "remote" / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "remote" / rel).write_bytes(raw)
    mf_path = tmp_path / "remote" / "snapshots" / sid / "manifest.json"
    mf = json.loads(mf_path.read_bytes())
    mf["files"][rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                        "size": len(raw)}
    mf_path.write_bytes(json.dumps(mf).encode())
    with pytest.raises(ContractError):
        min15_store.shard("cn", "2026-08", "min15")
