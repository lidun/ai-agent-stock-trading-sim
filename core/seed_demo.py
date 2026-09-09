"""演示数据种子（可选，opt-in）：让已交付页面在全新库上立即可见可演示。

用法：python -m core.seed_demo [--db core/data/aat.db]

- 走既有域写函数（strategy_profile.write_seed / strategy_memory._append /
  capability_center.register+bind / kb.create_entry），幂等：重复运行不重复落行。
- 仅用于本地演示/冒烟，不参与正式业务流程；生产库不自动执行（须显式传 --db）。
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from types import SimpleNamespace

from core.db import Connections, migrate, state_conn

DEMO = "agent-demo-001"
MANAGER = "agent-manager"


def _connect(db_path: str | Path) -> SimpleNamespace:
    path = Path(db_path)
    migrate(path)
    return SimpleNamespace(db=Connections(path))


def seed(state, *, demo: str = DEMO, manager: str = MANAGER) -> dict:
    """幂等填充演示数据，返回各域行数/项数（重复调用应保持稳定）。"""
    from core import capability_center, kb, strategy_memory, strategy_profile  # noqa: PLC0415

    # ---- 策略章程版本链（spec-06 §6.9 / spec-05 §7.1）----
    v1 = dict(
        core_belief="低波红利为底仓：股息率优先 + 波动率约束，防御型持有，"
                    "月度调仓候选 ≤ 5 只；核心理念锁定，变更需管理 Agent 授权留痕。",
        layers={
            "selection": "股息率 top 30 过滤，剔除质押比 > 50% 与 ST",
            "risk": "单票 ≤ 10%，组合波动率目标 6%-9%",
            "exit": "股息率跌破 2.5% 或 60 日新高回落 > 8% 触发复核",
        },
        note="初始章程（理念冻结 v1）",
    )
    v2 = dict(
        core_belief="低波红利为底仓（不变）：股息率优先 + 波动率约束；"
                    "卖出复核并入 3 日观察窗，避免单日波动误卖。",
        layers={
            "selection": "股息率 top 30 过滤，剔除质押比 > 50% 与 ST",
            "risk": "单票 ≤ 10%，组合波动率目标 6%-9%",
            "exit": "跌破阈值先进 3 日观察窗，连续确认才卖出",
        },
        note="v0.2：卖出复核 3 日观察窗（参考 KB-0001 拦截经验）",
    )
    for v in (v1, v2):
        strategy_profile.write_seed(
            state, demo,
            version_no="v0.1.0" if v is v1 else "v0.2.0",
            core_belief=v["core_belief"], layers=v["layers"], locked=True,
            charter_hash=hashlib.sha1(
                (v["core_belief"] + str(v["layers"])).encode()).hexdigest(),
            note=v["note"], active=(v is v2),
        )

    # ---- 演进记忆（spec-02 §3.1 type=strategy；spec-05 §4.1）----
    memory_seed = [
        dict(version_no="v0.1.0", ts="2026-09-01T10:00:00Z",
             body="诊断：回放显示 8 月两次单日 -1.5% 下行由突发情绪抛售触发，"
                  "非股息逻辑破坏（依据=持仓盈亏与当日成交）。",
             source="opt:demo-001", dedup_key="opt:demo-001"),
        dict(version_no="v0.2.0", ts="2026-09-08T10:00:00Z",
             body="受控修改：跌破阈值先进 3 日观察窗，连续确认才卖。"
                  "依据=v0.1.0 诊断 + KB-0001 尾盘缩量经验；预期=减少误卖、卖平占比提升。",
             source="opt:demo-002", dedup_key="opt:demo-002",
             ref_ids=["KB-0001"]),
    ]
    for m in memory_seed:
        strategy_memory._append(
            state, demo, version_no=m["version_no"], body=m["body"],
            ref_ids=m.get("ref_ids", []), source=m["source"],
            dedup_key=m["dedup_key"], ts=m["ts"])

    # ---- 能力市场（spec-05 §2）：注册 + 绑定 demo ----
    caps = [
        dict(name="移动均线趋势过滤", capability_type="tool", version="v1.2.0",
             description="MA 多头排列只读计算器，供选股过滤复用。",
             source_type="selfmade",
             source_ref=f"作者:{manager} · spec-05 §2 自研"),
        dict(name="中证红利指数日线源", capability_type="datasource",
             version="v1.0.0",
             description="红利指数/成分权重日线订阅（离线快照）。",
             source_type="opensource",
             source_ref="repo:example/hsia @ MIT"),
        dict(name="月度复盘模板", capability_type="skill", version="v1.0.0",
             description="月度归因 SKILL.md 模板（盈利/亏损/换手拆分）。",
             source_type="selfmade",
             source_ref=f"作者:{manager} · spec-05 §2 自研"),
    ]
    existing_caps = {c["name"]: c for c in capability_center.catalog(state)["items"]}
    bound_ids: list[str] = []
    for c in caps:
        cap = existing_caps.get(c["name"])
        if cap is None:
            cap = capability_center.register(state, **c, maintainer=manager)
        if c["capability_type"] in ("tool", "datasource"):
            capability_center.bind(state, capability_id=cap["id"], agent_id=demo)
            bound_ids.append(cap["id"])
    assert len(bound_ids) <= 2  # 幂等断言：目录只 3 项、tool+datasource 在绑

    # ---- 策略版本化演进链（spec-02 §9 / EVOQUANT）：v1 现役，v2 验证中 ----
    from core import strategy_versions  # noqa: PLC0415
    if strategy_versions.list_versions(state, demo)["total"] == 0:
        strategy_versions.checkpoint(
            state, demo, version_no="v1",
            config={
                "selection": {"filter": "dividend", "top": 30,
                              "exclude": ["st", "pledge>50"]},
                "risk": {"single_stock_cap": 0.1, "vol_target": [0.06, 0.09]},
                "exit": {"rule": "div_yield<2.5% 或 60日新高回落>8% 触发复核"},
            },
            basis=["opt:demo-001", "KB-0001"], trial_window={},
            created_by="manager")
        strategy_versions.activate(state, demo, "v1", activated_by="manager")
        strategy_versions.checkpoint(
            state, demo, version_no="v2",
            config={
                "selection": {"filter": "dividend", "top": 30,
                              "exclude": ["st", "pledge>50"]},
                "risk": {"single_stock_cap": 0.1, "vol_target": [0.06, 0.09]},
                "exit": {"rule": "跌破阈值先进 3 日观察窗，连续确认才卖"},
            },
            config_diff={"exit": {"window_days": 3}},
            basis=["opt:demo-002", "KB-0001"],
            trial_window={"window_days": 10, "cap_ceiling": 0.3},
            created_by="manager")

    # ---- 知识库（spec-05 §3）：正/反两例 ----
    existing_kb = {r["name"] for r in kb.list_entries(state)}
    spec_pit = {
        "trigger_rule": "收盘前 5 分钟涨幅 < 0 且成交额 < 近 20 日均值 50%",
        "computation": "按日内 5m 序列末段相邻分钟判定触达",
        "data_sources": ["l1_minute", "eod_daily"],
    }
    spec_pos = {
        "trigger_rule": "近 12 月股息率 > 4.5% 且分红连续 3 年未断",
        "computation": "以年报宣告日股息率 + 除权日成交回填",
        "data_sources": ["eod_daily"],
    }
    if "尾盘缩量走弱陷阱" not in existing_kb:
        kb.create_entry(state, name="尾盘缩量走弱陷阱", type_="pitfall",
                        description="尾盘无量阴跌多为资金退潮信号，不适合当日抄底；"
                                    "跌破阈值先观察（被 KB 演进记忆 v0.2 引用）。",
                        computable_spec=spec_pit, severity="high",
                        env_scope="all", source="retrospective",
                        origin_agent=demo, created_by=manager)
    if "红利因子股息率筛选" not in existing_kb:
        kb.create_entry(state, name="红利因子股息率筛选", type_="positive",
                        description="股息率与低波约束的防御样本池方向（验证中）。",
                        computable_spec=spec_pos, severity="low",
                        env_scope="all", source="manager_observation",
                        origin_agent=demo, created_by=manager)

    return {
        "charter_versions": state_conn(state).execute(
            "SELECT COUNT(*) n FROM strategy_charter_versions WHERE agent_id=?",
            (demo,)).fetchone()["n"],
        "strategy_versions": strategy_versions.list_versions(state, demo)["total"],
        "memory": len(strategy_memory.list_strategy_memory(state, demo)["items"]),
        "capabilities": len(capability_center.catalog(state)["items"]),
        "bindings": len(capability_center.agent_bindings(state, demo)["items"]),
        "kb": len(kb.list_entries(state)),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="演示数据种子（幂等）")
    ap.add_argument("--db", default="core/data/aat.db")
    args = ap.parse_args()
    state = _connect(args.db)
    counts = seed(state)
    print("seed ok:", counts)


if __name__ == "__main__":
    main()
