"""本地资源余量采样与准入判定（spec-04 §7.2 资源闸门模型）。

- 新任务投递门槛：CPU 与内存可用率**均 ≥30%**（可配）才投递新任务；
- 任务内 LLM 调用门槛：两者均 ≥10% 才投递下一个 LLM 调用。

采样基于 `/proc`（Linux），不做阻塞式 1 秒采样——用 loadavg 1 分钟均值近似 1 秒均值；
非 Linux/读取失败时返回保守的“未知”容量（准入交由调用方策略决定）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

NEW_TASK_THRESHOLD = 0.30
LLM_CALL_THRESHOLD = 0.10


def _meminfo() -> tuple[float, float] | None:
    try:
        vals: dict[str, float] = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                vals[key.strip()] = float(rest.strip().split()[0])
        total = vals.get("MemTotal", 0.0)
        avail = vals.get("MemAvailable", vals.get("MemFree", 0.0))
        if total <= 0:
            return None
        return total, avail
    except (OSError, ValueError):
        return None


def sample_capacity() -> dict:
    """采样本地资源余量；返回 CPU/内存可用率与原始量（比值 ∈ [0,1]）。"""
    cpu_count = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
        cpu_avail = max(0.0, min(1.0, 1.0 - load1 / cpu_count))
        known_cpu = True
    except (OSError, AttributeError):
        load1, cpu_avail, known_cpu = 0.0, 1.0, False

    mem = _meminfo()
    if mem is None:
        mem_avail_ratio, known_mem = 1.0, False
        mem_total_kb = mem_avail_kb = 0.0
    else:
        mem_total_kb, mem_avail_kb = mem
        mem_avail_ratio = max(0.0, min(1.0, mem_avail_kb / mem_total_kb))
        known_mem = True

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cpu_count": cpu_count, "load1": round(load1, 3),
        "cpu_avail_ratio": round(cpu_avail, 4),
        "mem_total_kb": int(mem_total_kb), "mem_avail_kb": int(mem_avail_kb),
        "mem_avail_ratio": round(mem_avail_ratio, 4),
        "known": known_cpu and known_mem,
    }


def admit_new_tasks(cap: dict, *, threshold: float = NEW_TASK_THRESHOLD) -> bool:
    """§7.2③：CPU 与内存可用率均 ≥ 阈值才准入新任务（备份高优先级例外由调用方处理）。"""
    return (cap["cpu_avail_ratio"] >= threshold
            and cap["mem_avail_ratio"] >= threshold)


def admit_llm_call(cap: dict, *, threshold: float = LLM_CALL_THRESHOLD) -> bool:
    """§7.2②：任务内 LLM 调用准入硬底线（两者均 ≥10%）。"""
    return (cap["cpu_avail_ratio"] >= threshold
            and cap["mem_avail_ratio"] >= threshold)
