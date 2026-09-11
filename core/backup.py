"""备份、恢复与重建（spec-02 §10，#17/#51）。

- **在线备份**：SQLite backup API（不暂停调度）；备份前检测 `runtime_flags.is_settling()`
  安全点，结算中则推迟重试（下个 tick），落 audit；
- **每日自动备份（最高优先级）**：本地备份 + 保留最近 `backup_retention` 份轮换，
  幂等按当日文件名（`aat-<YYYYMMDD>-*.db`）；core 长时停机后启动即补；
- **向量不随备份**：导出包只记 embedding 模型/版本（config），还原后由原文重建；
- **导出快照** `export_snapshot`：zip（业务库副本 + manifest 校验清单），不含向量库文件。
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import runtime_flags
from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

_BJT = timezone(timedelta(hours=8))
_BACKUP_GLOB = "aat-*.db"
_EXPORT_GLOB = "aat-export-*.zip"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _settings(state):
    return getattr(state, "settings", None)


def _audit(state, *, action: str, result: str, object_id: str, actor: str,
           detail: str) -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, action, "backup", object_id, result,
             detail[:400], ""))


def _db_path(state) -> Path:
    s = _settings(state)
    if s is not None and hasattr(s, "resolved_db_path"):
        return Path(s.resolved_db_path())
    from core.config import load_settings  # noqa: PLC0415
    return Path(load_settings().resolved_db_path())


def backup_dir(state) -> Path:
    s = _settings(state)
    sub = getattr(s, "backups_subdir", "backups") if s is not None else "backups"
    d = Path(_db_path(state)).parent / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def _retention(state) -> int:
    return max(1, int(getattr(_settings(state), "backup_retention", 7) or 7))


def _bj_tag(now_utc: datetime) -> str:
    bj = now_utc.astimezone(_BJT)
    return bj.strftime("%Y%m%d-%H%M%S")


def list_backups(state) -> list[dict]:
    d = backup_dir(state)
    out = []
    for f in sorted(d.glob(_BACKUP_GLOB)):
        st = f.stat()
        out.append({"name": f.name, "path": str(f), "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                    .isoformat(timespec="seconds")})
    return out


def _today_tag(state, now_utc: datetime) -> str:
    return now_utc.astimezone(_BJT).strftime("%Y%m%d")


def _today_done(state, now_utc: datetime) -> bool:
    return any(backup_dir(state).glob(f"aat-{_today_tag(state, now_utc)}-*.db"))


def _prune(state) -> list[str]:
    """轮换清理：仅删除本目录内 `aat-*.db` 备份，保留最近 retention 份。"""
    files = sorted(backup_dir(state).glob(_BACKUP_GLOB))
    keep = _retention(state)
    removed: list[str] = []
    for f in files[:-keep] if len(files) > keep else []:
        try:
            f.unlink()
            removed.append(f.name)
        except OSError:
            log.warning("备份轮换删除失败：%s", f)
    return removed


def create_backup(state, *, now: datetime | None = None,
                  actor: str = "scheduler") -> dict:
    """执行一次在线备份（SQLite backup API）。结算中不调用（由调用方先检测）。"""
    now_utc = now or datetime.now(timezone.utc)
    src_path = _db_path(state)
    dest = backup_dir(state) / f"aat-{_bj_tag(now_utc)}-{secrets.token_hex(2)}.db"
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    removed = _prune(state)
    size = dest.stat().st_size
    _audit(state, action="backup.daily", result="ok", object_id=dest.name,
           actor=actor, detail=f"{size} bytes，轮换删除 {len(removed)} 份")
    return {"status": "done", "path": str(dest), "name": dest.name, "size": size,
            "pruned": removed, "retention": _retention(state)}


def maybe_daily_backup(state, *, now: datetime | None = None,
                       actor: str = "scheduler") -> dict:
    """当日尚未备份则执行；结算中推迟（§10 安全点）。幂等。"""
    now_utc = now or datetime.now(timezone.utc)
    if _today_done(state, now_utc):
        return {"status": "already", "tag": _today_tag(state, now_utc)}
    if runtime_flags.is_settling():
        _audit(state, action="backup.daily", result="deferred", object_id="",
               actor=actor, detail="结算中，备份推迟至下个安全点")
        return {"status": "deferred", "reason": "settling"}
    return create_backup(state, now=now_utc, actor=actor)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def export_snapshot(state, *, now: datetime | None = None,
                    out_path: str | Path | None = None,
                    actor: str = "ui") -> dict:
    """导出快照 zip（业务库副本 + manifest 校验清单）；不含向量库。"""
    now_utc = now or datetime.now(timezone.utc)
    s = _settings(state)
    db_bytes_path = _db_path(state)
    src = sqlite3.connect(str(db_bytes_path))
    tmp = backup_dir(state) / f".export-{secrets.token_hex(4)}.db"
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
        db_bytes = tmp.read_bytes()
    finally:
        src.close()
        tmp.unlink(missing_ok=True)
    manifest = {
        "created_ts": _now_iso(),
        "core_version": _core_version(),
        "db_sha256": _sha256(db_bytes),
        "db_size": len(db_bytes),
        "embedding_model": getattr(s, "embedding_model", "") if s else "",
        "embedding_version": getattr(s, "embedding_version", "") if s else "",
        "contains_vector": False,
        "note": "不含向量库；还原后由原文重建（spec-02 §10）",
    }
    dest = Path(out_path) if out_path else \
        backup_dir(state) / f"aat-export-{_bj_tag(now_utc)}-{secrets.token_hex(2)}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("aat.db", db_bytes)
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    _audit(state, action="backup.export", result="ok", object_id=dest.name,
           actor=actor, detail=f"导出快照 {dest.stat().st_size} bytes，含清单校验")
    return {"status": "exported", "path": str(dest), "name": dest.name,
            "size": dest.stat().st_size, "manifest": manifest}


def verify_snapshot(zip_path: str | Path) -> dict:
    """校验导出快照清单与库文件哈希。"""
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        if not {"aat.db", "manifest.json"} <= names:
            raise ValueError("快照缺少 aat.db 或 manifest.json")
        manifest = json.loads(z.read("manifest.json"))
        digest = _sha256(z.read("aat.db"))
    ok = digest == manifest.get("db_sha256")
    return {"ok": ok, "manifest": manifest, "actual_sha256": digest}


def _core_version() -> str:
    try:
        from core import __version__  # noqa: PLC0415
        return __version__
    except Exception:  # noqa: BLE001
        return ""
