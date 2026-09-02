"""Durable background runner for multilingual translation and CMS publishing."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List

from db.database import BLOG_LANGS, BLOG_TARGET_LANGS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = PROJECT_ROOT / "runtime" / "workflow_runs"
STAGES = {"translate", "publish"}


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _read(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _pid_running(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    stat_path = Path(f"/proc/{value}/stat")
    try:
        if stat_path.is_file() and stat_path.read_text(encoding="utf-8").split()[2] == "Z":
            return False
    except (OSError, IndexError):
        pass
    try:
        os.kill(value, 0)
        return True
    except OSError:
        return False


def _lock_path(stage: str) -> Path:
    return RUN_DIR / f"{stage}.lock"


def _normalize(stage: str, selections: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    allowed = BLOG_TARGET_LANGS if stage == "translate" else BLOG_LANGS
    normalized: List[Dict[str, str]] = []
    seen = set()
    for item in selections or []:
        job_id = str(item.get("job_id") or "").strip()
        lang = str(item.get("lang") or "").strip().lower()
        key = (job_id, lang)
        if not job_id or lang not in allowed or key in seen:
            continue
        seen.add(key)
        normalized.append({"job_id": job_id, "lang": lang})
    return normalized


def _active(stage: str) -> Dict[str, Any]:
    lock = _lock_path(stage)
    if not lock.exists():
        return {}
    lock_data = _read(lock)
    state_path = Path(str(lock_data.get("state_path") or ""))
    state = _read(state_path) if state_path.is_file() else {}
    if state.get("status") == "running":
        starting = not state.get("pid") and time.time() - float(state.get("started_at") or 0) < 10
        if starting or _pid_running(state.get("pid")):
            return state
    if state and state.get("status") == "running":
        state.update(status="failed", finished_at=time.time())
        state.setdefault("errors", []).append("任务进程意外中断")
        _atomic_write(state_path, state)
    try:
        lock.unlink()
    except OSError:
        pass
    return {}


def start_workflow(
    stage: str,
    selections: Iterable[Dict[str, Any]],
    *,
    owner_line: str = "",
    owner_username: str = "",
) -> Dict[str, Any]:
    if stage not in STAGES:
        return {"ok": False, "error": "未知流水线阶段"}
    items = _normalize(stage, selections)
    if not items:
        return {"ok": False, "error": "没有可处理的文章版本"}
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(stage)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        active = _active(stage)
        if active:
            return {"ok": False, "error": f"{stage} 任务正在运行"}
        return start_workflow(
            stage,
            items,
            owner_line=owner_line,
            owner_username=owner_username,
        )

    run_id = uuid.uuid4().hex[:10]
    state_path = RUN_DIR / f"{run_id}.state.json"
    selections_path = RUN_DIR / f"{run_id}.selections.json"
    state = {
        "run_id": run_id, "stage": stage, "status": "running", "pid": None,
        "total": len(items), "completed": 0, "failed": 0, "current": None,
        "errors": [], "started_at": time.time(), "finished_at": None,
        "owner_line": str(owner_line or ""),
        "owner_username": str(owner_username or ""),
    }
    _atomic_write(state_path, state)
    selections_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.write(fd, json.dumps({"run_id": run_id, "state_path": str(state_path)}).encode("utf-8"))
    os.close(fd)
    log_path = RUN_DIR / f"{run_id}.log"
    log_handle = open(log_path, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, "workflow_worker.py", "--stage", stage,
             "--selections", str(selections_path), "--state", str(state_path),
             "--lock", str(lock)],
            cwd=str(PROJECT_ROOT), stdout=log_handle, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
    except Exception:
        log_handle.close()
        lock.unlink(missing_ok=True)
        raise
    log_handle.close()
    return {"ok": True, "run_id": run_id, "stage": stage, "total": len(items)}


def workflow_status() -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for stage in sorted(STAGES):
        active = _active(stage)
        if active:
            result[stage] = active
            continue
        states = []
        for path in RUN_DIR.glob("*.state.json"):
            state = _read(path)
            if state.get("stage") == stage:
                states.append(state)
        states.sort(key=lambda item: float(item.get("started_at") or 0), reverse=True)
        result[stage] = states[0] if states else {
            "stage": stage, "status": "idle", "total": 0,
            "completed": 0, "failed": 0, "current": None, "errors": [],
        }
    return result
