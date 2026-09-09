"""数据源适配器注册与分层选择（spec-03 §2.1/§2.2，v0.4 分层规则）。

- 默认优先级链 = ftai.chat → akshare → 东财直连 → 新浪/腾讯直连兜底；按链顺序只在
  “已配置且可用”的候选中解析本角色的源。当前实现适配器仅腾讯系（quotes_tencent，
  source_family='tencent'），东财/akshare/新浪/ftai 作为 spec 链上的候选源登记
  （family/角色口径齐备，availability=False，实测/接入通过前不进候选）。
- 采集类（L0）当日只退避不切源（单源同日不变式由 collector 用 source_binding 强制）；
  拉取类（L1/L2）连续失败当日自动切下一候选（SourceRouter，对新拉取生效）。
- source_family 映射表供交叉抽检异族校验（§7 B2：同底层抽检是假跨源）。
"""
from __future__ import annotations

from datetime import date
from importlib import import_module

PRIORITY = ["ftai", "akshare", "eastmoney", "sina", "tencent"]

FAMILIES = {
    "ftai": "ftai",
    "akshare": "eastmoney",
    "eastmoney": "eastmoney",
    "sina": "sina",
    "tencent": "tencent",
}

ROLES = {name: {"l0", "l1", "l2"} for name in PRIORITY}

# 已实现/可加载的适配器；availability=False 仅登记 spec 候选语义，未接入不加载。
_IMPLEMENTED = {"tencent": "core.quotes_tencent"}


class SourceChainError(ValueError):
    """源注册/配置错误。"""


def configured_sources(raw: str) -> list[str]:
    """解析运行期“已配置源”清单（CORE_MARKET_SOURCES）。

    保留 spec §2.2 链顺序；出现未登记源名 → 明确报错（配置笔误早暴露，不静默忽略）。
    """
    names = [n.strip() for n in (raw or "").split(",") if n.strip()]
    bad = [n for n in names if n not in FAMILIES]
    if bad:
        raise SourceChainError(f"未登记的数据源: {', '.join(bad)}（已知: {', '.join(PRIORITY)}）")
    ordered = {n: i for i, n in enumerate(PRIORITY)}
    return sorted(set(names), key=lambda n: ordered[n])


def family_of(name: str) -> str:
    if name not in FAMILIES:
        raise SourceChainError(f"未登记的数据源: {name}")
    return FAMILIES[name]


def roles_of(name: str) -> set[str]:
    return set(ROLES.get(name, set()))


def adapter_for(name: str):
    """加载已实现适配器模块（未实现源 → SourceChainError，不尝试构造）。"""
    mod_name = _IMPLEMENTED.get(name)
    if mod_name is None:
        raise SourceChainError(f"数据源 {name} 尚未接入适配器（spec-03 §11 验证后接入）")
    return import_module(mod_name)


def resolve(configured: list[str], role: str, *, exclude: set[str] | None = None) -> str | None:
    """按链顺序返回角色可用的首个源；无可用 → None（调用方按缺源处理）。"""
    exclude = exclude or set()
    for name in configured:
        if name in exclude:
            continue
        if role in roles_of(name):
            return name
    return None


# 拉取类当日连续失败降权阈值（§9 C 级语义的最小实现：失败 ≥3 次 → 当日降权切源）
FAIL_TO_QUARANTINE = 3


class SourceRouter:
    """拉取类（L1/L2 日线/分钟线/参考数据）当日自动切换路由（spec-02 §2.2 分层规则）。

    候选按 spec 链顺序；某源当日连续失败达阈值 → 当日降权（切下一候选，对新拉取生效，
    不做拉取中切换）；次日自然回权（冷却期为 0，跨进程由日切重建）。
    """

    def __init__(self, configured: list[str], role: str, *, fail_to_quarantine: int = FAIL_TO_QUARANTINE):
        if role not in ("l0", "l1", "l2"):
            raise ValueError(f"未知数据角色: {role}")
        self.configured = [c for c in configured if role in roles_of(c)]
        self.role = role
        self.fail_to_quarantine = fail_to_quarantine
        self._fails: dict[str, int] = {c: 0 for c in self.configured}
        self._quarantined: set[str] = set()
        self._day: str | None = None

    def pick(self) -> str | None:
        for name in self.configured:
            if name not in self._quarantined:
                return name
        return None

    def note_day(self, day: date | str | None) -> None:
        d = day.isoformat() if isinstance(day, date) else day
        if d is not None and d != self._day:
            self._day = d
            self._quarantined.clear()
            self._fails = {c: 0 for c in self.configured}

    def record_ok(self, name: str) -> None:
        self._fails[name] = 0

    def record_failure(self, name: str) -> str | None:
        """记录失败；触发当日降权时返回下一候选（可 None=候选耗尽）。"""
        self._fails[name] = self._fails.get(name, 0) + 1
        if self._fails[name] >= self.fail_to_quarantine:
            self._quarantined.add(name)
        return self.pick()
