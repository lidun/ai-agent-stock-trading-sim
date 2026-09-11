"""备份/导出测试（spec-02 §10）。"""
from __future__ import annotations

import zipfile
from datetime import datetime, timedelta, timezone

from core import backup, runtime_flags, tasks
from core.tests.conftest import csrf_headers

FUTURE = datetime(2030, 1, 1, 4, 0, tzinfo=timezone.utc)


def test_create_and_list(authed_client):
    st = authed_client.app.state
    r = backup.create_backup(st, now=FUTURE)
    assert r["status"] == "done" and r["size"] > 0
    names = {b["name"] for b in backup.list_backups(st)}
    assert r["name"] in names


def test_retention_rotation(authed_client):
    st = authed_client.app.state
    st.settings.backup_retention = 2
    for i in range(3):
        backup.create_backup(st, now=FUTURE + timedelta(seconds=i))
    assert len(backup.list_backups(st)) <= 2


def test_maybe_daily_idempotent(authed_client):
    st = authed_client.app.state
    first = backup.maybe_daily_backup(st, now=FUTURE)
    assert first["status"] == "done"
    second = backup.maybe_daily_backup(st, now=FUTURE)
    assert second["status"] == "already"


def test_settling_defers_backup(authed_client):
    st = authed_client.app.state
    now = FUTURE + timedelta(days=1)
    with runtime_flags.settling():
        assert backup.maybe_daily_backup(st, now=now)["status"] == "deferred"
    assert backup.maybe_daily_backup(st, now=now)["status"] == "done"


def test_export_and_verify(authed_client):
    st = authed_client.app.state
    r = backup.export_snapshot(st, now=FUTURE)
    assert r["manifest"]["contains_vector"] is False
    v = backup.verify_snapshot(r["path"])
    assert v["ok"] is True
    with zipfile.ZipFile(r["path"]) as z:
        assert {"aat.db", "manifest.json"} <= set(z.namelist())


def test_backup_task_handler(authed_client):
    st = authed_client.app.state
    r = tasks.enqueue(st, task_type="备份", dedup_key="manual-test",
                      resource_class="scan")
    out = tasks.run_deferrable(st, limit=10)
    assert tasks.get_task(st, r["id"])["status"] == "done"


def test_backup_routes(authed_client):
    run = authed_client.post("/api/backup/run", headers=csrf_headers(authed_client))
    assert run.status_code == 200 and run.json()["status"] == "done"
    lst = authed_client.get("/api/backup/list")
    assert lst.status_code == 200 and lst.json()["items"]
    exp = authed_client.post("/api/backup/export", headers=csrf_headers(authed_client))
    assert exp.status_code == 200 and exp.json()["manifest"]["contains_vector"] is False
    assert run.json()["size"] > 0
