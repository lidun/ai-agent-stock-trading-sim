"""spec-03 §2 数据源分层测试：链顺序/解析排除/未接入适配器报错/拉取类当日降权切换。"""
from __future__ import annotations

import pytest

from core import source_chain
from core.source_chain import SourceRouter


def test_configured_sources_keeps_spec_priority():
    cfg = source_chain.configured_sources("tencent,akshare,eastmoney")
    assert cfg == ["akshare", "eastmoney", "tencent"]


def test_configured_sources_rejects_unknown():
    with pytest.raises(source_chain.SourceChainError):
        source_chain.configured_sources("tencent,mystery")


def test_resolve_by_role_and_exclude():
    cfg = source_chain.configured_sources("tencent,eastmoney")
    assert source_chain.resolve(cfg, "l0") == "eastmoney"
    assert source_chain.resolve(cfg, "l1", exclude={"eastmoney"}) == "tencent"
    assert source_chain.resolve([], "l0") is None


def test_family_mapping_and_adapter_gate():
    assert source_chain.family_of("tencent") == "tencent"
    assert source_chain.family_of("akshare") == "eastmoney"   # 同底层归属（B2 异族口径）
    with pytest.raises(source_chain.SourceChainError):
        source_chain.adapter_for("akshare")                    # 未接入不构造
    from core import quotes_tencent
    assert source_chain.adapter_for("tencent") is quotes_tencent


def test_router_quarantine_on_consecutive_fail_then_next_day_reset():
    cfg = source_chain.configured_sources("tencent,eastmoney")
    router = SourceRouter(cfg, "l1")
    assert router.pick() == "eastmoney"
    assert router.record_failure("eastmoney") == "eastmoney"   # 1 次未降权
    assert router.record_failure("eastmoney") == "eastmoney"   # 2 次未降权
    assert router.record_failure("eastmoney") == "tencent"     # 第 3 次 → 切下一候选
    assert router.pick() == "tencent"
    router.record_ok("tencent")
    assert router.pick() == "tencent"
    router.record_failure("tencent")                            # 候选耗尽不抛
    router.note_day("2026-09-10")                               # 次日回权
    assert router.pick() == "eastmoney"
