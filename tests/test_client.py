"""传输层测试：镜像降级、篡改拒绝、离线缓存、pinned 复现。

这些路径是整个「数据一定能用」立论的实现处，必须跑在**真实字节**上。
"""
from __future__ import annotations

import json
import shutil

import pytest

import ohlcvault as ov
from ohlcvault.client import MarketClient

from conftest import first_shard, manifest_of


def client(mirrors, cache, **kw):
    c = MarketClient(mirrors, cache, **kw)
    c.trace.clear()
    return c


def test_primary_mirror_used_when_all_online(mirrors, tmp_path):
    ms, base = mirrors
    c = client(ms, tmp_path / "c1")
    rel = first_shard(base / "cloudflare_r2")
    sha = manifest_of(base / "cloudflare_r2")["files"][rel]["sha256"]
    assert ov.sha256_bytes(c.raw(rel)) == sha
    assert c.stats["hash_reject"] == 0
    assert c.stats["mirror_fail"] == 0
    # 注意 served_by 也含控制面（latest.json / manifest.json），不能只断言 ==1
    assert "cloudflare_r2" in c.stats["served_by"]


def test_falls_back_when_primary_down(mirrors, tmp_path):
    ms, base = mirrors
    shutil.rmtree(base / "cloudflare_r2")
    c = client(ms, tmp_path / "c2")
    rel = first_shard(base / "jsdelivr")
    c.raw(rel)
    assert c.stats["mirror_fail"] >= 1
    assert "jsdelivr" in c.stats["served_by"]


def test_successful_mirror_is_promoted(mirrors, tmp_path):
    """成功过的镜像提到首位 —— 否则每次请求都要先付一次失败超时。"""
    ms, base = mirrors
    shutil.rmtree(base / "cloudflare_r2")
    c = client(ms, tmp_path / "c3")
    c.raw(first_shard(base / "jsdelivr"))
    assert c.mirrors[0].endswith("jsdelivr")


def test_tampered_mirror_is_rejected_not_served(mirrors, tmp_path):
    """**这是校验存在的唯一理由。**

    一个镜像返回的字节若与快照清单的 sha256 不符，它必须被丢弃并降级，
    绝不能把脏数据交给调用方 —— 哪怕它看起来完全正常。
    """
    ms, base = mirrors
    rel = first_shard(base / "cloudflare_r2")
    bad = base / "cloudflare_r2" / rel
    raw = bytearray(bad.read_bytes())
    raw[len(raw) // 2] ^= 0xFF                 # 静默改动一个字节
    bad.write_bytes(bytes(raw))

    c = client(ms, tmp_path / "c4")
    body = c.raw(rel)
    assert c.stats["hash_reject"] == 1
    assert ov.sha256_bytes(body) == \
        manifest_of(base / "jsdelivr")["files"][rel]["sha256"]
    # 脏数据来自首选镜像，被拒后才降级 —— trace 里应留下明确的拒绝记录
    assert any("内容与清单不符" in t for t in c.trace)


def test_all_mirrors_tampered_raises(mirrors, tmp_path):
    """全部镜像都不可信 → 报错。宁可不给数据，也不给脏数据。"""
    ms, base = mirrors
    rel = first_shard(base / "cloudflare_r2")
    for name in ("cloudflare_r2", "jsdelivr", "github_raw"):
        p = base / name / rel
        raw = bytearray(p.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        p.write_bytes(bytes(raw))
    c = client(ms, tmp_path / "c5")
    with pytest.raises(ov.SnapshotUnavailable):
        c.raw(rel)
    assert c.stats["hash_reject"] == 3


def test_offline_serves_from_cache(mirrors, tmp_path):
    """已拉过的数据永久可用 —— 缓存有效性由哈希决定，与时间无关。"""
    ms, base = mirrors
    cache = tmp_path / "c6"
    rel = first_shard(base / "cloudflare_r2")
    first = client(ms, cache).raw(rel)

    for name in ("cloudflare_r2", "jsdelivr", "github_raw"):
        shutil.rmtree(base / name)

    c2 = client(ms, cache)
    assert c2.raw(rel) == first              # 逐字节一致
    assert c2.stats["cache_hit"] == 1


def test_offline_without_cache_raises(mirrors, tmp_path):
    ms, base = mirrors
    for name in ("cloudflare_r2", "jsdelivr", "github_raw"):
        shutil.rmtree(base / name)
    c = client(ms, tmp_path / "c7")
    with pytest.raises(ov.SnapshotUnavailable):
        c.manifest()


def test_pinned_snapshot_does_not_follow_latest(mirrors, tmp_path):
    """pinned 模式是复现研究的入口：指定快照 id 后不跟随 latest.json。"""
    ms, base = mirrors
    sid = json.loads((base / "cloudflare_r2" / "latest.json").read_bytes())["snapshot"]
    c = client(ms, tmp_path / "c8", snapshot=sid)
    assert c.snapshot == sid
    rel = first_shard(base / "cloudflare_r2")
    assert ov.sha256_bytes(c.raw(rel)) == \
        manifest_of(base / "cloudflare_r2")["files"][rel]["sha256"]


def test_unknown_file_raises_keyerror(mirrors, tmp_path):
    ms, _ = mirrors
    with pytest.raises(KeyError):
        client(ms, tmp_path / "c9").raw("daily/cn/1899-01.json.gz")


def test_requires_at_least_one_mirror(tmp_path):
    with pytest.raises(ValueError):
        MarketClient([], tmp_path / "c10")


def test_local_directory_is_a_valid_mirror(data_dir, tmp_path):
    """本地目录即合法镜像 —— 离线复现与自建镜像站不需要额外代码。"""
    ov.reset()
    st = ov.connect(mirrors=[str(data_dir)], cache_dir=tmp_path / "c11", quiet=True)
    assert st.available_periods("cn")


def test_env_var_configured_mirrors(data_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("OHLCVAULT_MIRRORS", str(data_dir))
    monkeypatch.setenv("OHLCVAULT_CACHE", str(tmp_path / "c12"))
    ov.reset()
    st = ov.connect(quiet=True)
    assert st.available_periods("cn")
    ov.reset()
