"""进程内运行标志（spec-02 §10 安全点检测）。

结算事务期间持有"结算中"标志，备份任务据此推迟重试（不做"暂停调度"式粗暴备份）。
单进程内通过线程锁保护计数，暴露 `is_settling()` 供备份前检测。
"""
from __future__ import annotations

import contextlib
import threading

_lock = threading.Lock()
_active = 0


def begin_settling() -> None:
    global _active
    with _lock:
        _active += 1


def end_settling() -> None:
    global _active
    with _lock:
        _active = max(0, _active - 1)


def is_settling() -> bool:
    with _lock:
        return _active > 0


@contextlib.contextmanager
def settling():
    begin_settling()
    try:
        yield
    finally:
        end_settling()
