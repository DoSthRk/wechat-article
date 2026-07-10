"""操作面板后端：列内容源（各线 PDF + 处理状态）+ 后台跑流水线（子进程，单活跃任务）。

设计：
- 内容源 = 各 line 的 ``extra.pdf_folder`` 指向 ``inputs/pdfs/{folder}/`` 下的 PDF。
- 跑流水线复用现成 CLI（``batch_processor.py``）：面板把选中的 PDF 拼成临时 jobs.yaml，
  起一个子进程跑 ``--stage all``，输出写日志文件，面板轮询日志 + 进程状态。
- **单活跃任务**：同一时刻只允许一个子进程在跑（避免 SQLite 并发写冲突）；UI 据此禁用按钮。
"""
from __future__ import annotations

import os
import re
import json
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from utils.line_loader import LineLoadError, load_line_by_id
from utils.logger import setup_logger

logger = setup_logger("panel_runner")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LINES_DIR = PROJECT_ROOT / "inputs" / "lines"
PDFS_DIR = PROJECT_ROOT / "inputs" / "pdfs"
RUN_DIR = PROJECT_ROOT / "runtime" / "panel_runs"
_DEFAULT_MAX_PAGES = 20
_OPERATOR_PENDING_FILE = "operator_pending.json"


def _job_id_from_pdf(pdf_stem: str) -> str:
    """PDF 文件名 → job_id（去空格，保留中文；作 outputs/jobs 目录名 + DB 键）。"""
    return re.sub(r"\s+", "-", pdf_stem.strip())


def _safe_pdf_name(filename: str) -> Optional[str]:
    """规整上传文件名为安全的纯文件名（防目录穿越）：取末段、必须 .pdf、保留中文。"""
    name = (filename or "").replace("\\", "/").split("/")[-1].strip().replace("\x00", "")
    if not name.lower().endswith(".pdf"):
        return None
    if name in {".", ".."} or "/" in name or "\\" in name:
        return None
    if not name[:-4].strip():  # 形如 ".pdf"，无主名（pathlib 会把 ".pdf" 当 dotfile）
        return None
    return name


def save_uploaded_pdf(line_id: str, filename: str, data: bytes) -> dict:
    """把上传的 PDF 存到该线 ``pdf_folder`` 目录下。返回 {ok, name/pdf/job_id/overwrite/error}。"""
    line_id = (line_id or "").strip()
    try:
        line = load_line_by_id(str(LINES_DIR), line_id)
    except LineLoadError as exc:
        return {"ok": False, "error": f"线配置无效：{exc}"}
    folder = str((line.extra or {}).get("pdf_folder") or "").strip()
    if not folder:
        return {"ok": False, "error": "该线未配置 pdf_folder"}
    name = _safe_pdf_name(filename)
    if not name:
        return {"ok": False, "error": f"文件名非法或不是 PDF：{filename!r}"}
    if not data:
        return {"ok": False, "error": "空文件"}
    if b"%PDF-" not in data[:1024]:
        return {"ok": False, "error": "不是有效的 PDF（缺少 %PDF 头）"}

    dest_dir = PDFS_DIR / folder
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        overwrite = dest.exists()
        dest.write_bytes(data)
    except OSError as exc:
        return {"ok": False, "error": f"保存失败：{exc}"}
    logger.info(
        "panel upload: line=%s file=%s bytes=%d overwrite=%s", line_id, name, len(data), overwrite,
    )
    rel = _display_pdf_path(dest)
    already_generated = _is_generated_pdf(rel)
    if already_generated:
        _mark_operator_pending(line_id, rel, name)
    return {
        "ok": True, "name": name, "overwrite": overwrite,
        "already_generated": already_generated,
        "operator_pending": already_generated,
        "pdf": rel, "job_id": _job_id_from_pdf(dest.stem),
    }


def _is_generated_pdf(pdf_path: str) -> bool:
    """判断 PDF 是否已经绑定过生成文章；用于重复上传时给业务页明确反馈。"""
    try:
        from db.database import get_db_manager
        items, _ = get_db_manager().list_article_overview(page=1, page_size=500)
    except Exception:
        return False
    target_key = _norm_pdf_key(pdf_path)
    target_name = Path(pdf_path).name.lower()
    for item in items:
        if not item.get("title"):
            continue
        item_path = item.get("pdf_path") or ""
        if _norm_pdf_key(item_path) == target_key or Path(item_path).name.lower() == target_name:
            return True
    return False


def _operator_pending_path() -> Path:
    return RUN_DIR / _OPERATOR_PENDING_FILE


def _load_operator_pending() -> List[dict]:
    try:
        raw = _operator_pending_path().read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_operator_pending(items: List[dict]) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _operator_pending_path().write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _mark_operator_pending(line_id: str, pdf: str, name: str) -> None:
    items = _load_operator_pending()
    key = _norm_pdf_key(pdf)
    kept = [
        item for item in items
        if not (item.get("line_id") == line_id and _norm_pdf_key(item.get("pdf") or "") == key)
    ]
    kept.append({
        "line_id": line_id,
        "pdf": pdf,
        "name": name,
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    })
    _save_operator_pending(kept)


def _unmark_operator_pending(line_id: str, pdf: str) -> bool:
    items = _load_operator_pending()
    key = _norm_pdf_key(pdf)
    kept = [
        item for item in items
        if not (item.get("line_id") == line_id and _norm_pdf_key(item.get("pdf") or "") == key)
    ]
    if len(kept) == len(items):
        return False
    _save_operator_pending(kept)
    return True


def _operator_pending_keys(line_id: str) -> set[str]:
    return {
        _norm_pdf_key(item.get("pdf") or "")
        for item in _load_operator_pending()
        if item.get("line_id") == line_id
    }


def delete_pending_pdf(line_id: str, pdf: str) -> dict:
    """删除该内容线下尚未生成的 PDF。业务页只把待处理文件交给这里。"""
    line_id = (line_id or "").strip()
    try:
        line = load_line_by_id(str(LINES_DIR), line_id)
    except LineLoadError as exc:
        return {"ok": False, "error": f"线配置无效：{exc}"}
    folder = str((line.extra or {}).get("pdf_folder") or "").strip()
    if not folder:
        return {"ok": False, "error": "该线未配置 pdf_folder"}

    line_dir = (PDFS_DIR / folder).resolve()
    target = (PROJECT_ROOT / str(pdf)).resolve() if not Path(str(pdf)).is_absolute() else Path(str(pdf)).resolve()
    try:
        target.relative_to(line_dir)
    except ValueError:
        return {"ok": False, "error": "该文件不属于当前内容线"}
    if target.suffix.lower() != ".pdf":
        return {"ok": False, "error": "只能删除 PDF 文件"}
    if not target.exists():
        return {"ok": False, "error": "文件不存在"}

    target_display = _display_pdf_path(target)
    if _unmark_operator_pending(line_id, target_display):
        return {
            "ok": True, "name": target.name, "pdf": target_display,
            "removed_from_pending": True,
        }

    # 防止从业务页删掉已经绑定文章的历史素材。
    for line_info in list_sources():
        if line_info.get("line_id") != line_id:
            continue
        for item in line_info.get("pdfs", []):
            if _norm_pdf_key(item.get("pdf") or "") == _norm_pdf_key(str(target)):
                if item.get("has_article"):
                    return {"ok": False, "error": "该文件已生成内容，请在管理员版处理"}
                break

    try:
        target.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"删除失败：{exc}"}
    logger.info("panel delete: line=%s file=%s", line_id, target.name)
    return {"ok": True, "name": target.name, "pdf": target_display, "removed_from_pending": False}


def _line_ids() -> List[str]:
    return sorted(p.stem for p in LINES_DIR.glob("*.yaml")) if LINES_DIR.is_dir() else []


def _norm_pdf_key(path: str) -> str:
    """把 PDF 路径统一成「项目内相对 posix 小写」做匹配键（吃绝对/相对、正反斜杠、大小写）。"""
    pp = Path(path)
    try:
        return pp.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix().lower()
    except (ValueError, OSError):
        return pp.name.lower()  # 项目外 / 解析失败 → 退化到文件名


def _display_pdf_path(path: Path) -> str:
    """生产返回项目相对路径；测试/外部目录退化为绝对 posix 路径。"""
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return path.as_posix()


def list_sources() -> List[dict]:
    """各内容线的 PDF 列表 + 真实绑定（按 Job.pdf_path 匹配文章）+ 处理状态。"""
    from db.database import get_db_manager

    items, _ = get_db_manager().list_article_overview(page=1, page_size=500)
    # 按源 PDF 路径建索引；items 已按 job.id 倒序，首次命中即最新一篇
    by_pdf: Dict[str, dict] = {}
    for it in items:
        key = _norm_pdf_key(it.get("pdf_path") or "")
        if key and key not in by_pdf:
            by_pdf[key] = it
    out: List[dict] = []
    for line_id in _line_ids():
        try:
            line = load_line_by_id(str(LINES_DIR), line_id)
        except LineLoadError:
            continue
        folder = str((line.extra or {}).get("pdf_folder") or "").strip()
        d = PDFS_DIR / folder if folder else None
        pdfs = sorted(d.glob("*.pdf")) if d and d.is_dir() else []
        operator_pending = _operator_pending_keys(line_id)
        files = []
        for p in pdfs:
            display_pdf = _display_pdf_path(p)
            info = by_pdf.get(_norm_pdf_key(str(p)))
            dists = info.get("distributions", []) if info else []
            op_pending = _norm_pdf_key(display_pdf) in operator_pending
            already_generated = bool(info and info.get("title"))
            drafted = any(x.get("publish_status") == "published" for x in dists)
            needs_action = (not already_generated) or op_pending
            files.append({
                "pdf": display_pdf,
                "name": p.name,
                # 绑定到则用文章真实 job_id（预览/状态用）；否则用文件名推导值（仅供新跑）
                "job_id": (info.get("job_id") if info else _job_id_from_pdf(p.stem)),
                "bound": bool(info),
                "has_article": already_generated,
                "already_generated": already_generated,
                "operator_pending": op_pending,
                "needs_action": needs_action,
                "title": (info.get("title") if info else None),
                "published": drafted,
                "blocked": bool(info.get("publish_blocked")) if info else False,
            })
        counts = {
            "total": len(files),
            "pending": sum(1 for f in files if f["needs_action"]),
            "processed": sum(1 for f in files if f["has_article"]),
            "published": sum(1 for f in files if f["published"]),
            "blocked": sum(1 for f in files if f["blocked"]),
        }
        out.append({
            "line_id": line_id,
            "name": line.name,
            "account": str((line.extra or {}).get("wechat_account") or ""),
            "folder": folder,
            "counts": counts,
            "pdfs": files,
        })
    return out


@dataclass
class _Run:
    run_id: str
    task: str
    line_id: str
    jobs: List[str]
    log_path: str
    proc: Any = None
    status: str = "running"  # running | done | failed | cancelled
    started: float = field(default_factory=time.time)

    def serialize(self, tail: int = 60) -> dict:
        log_lines = _tail(self.log_path, tail)
        return {
            "run_id": self.run_id, "task": self.task, "line_id": self.line_id,
            "jobs": self.jobs, "status": self.status,
            "started": time.strftime("%H:%M:%S", time.localtime(self.started)),
            "summary": _summarize_log(log_lines, self.status, len(self.jobs)),
            "log": log_lines,
        }


_lock = threading.Lock()
_current: Optional[_Run] = None
_history: List[_Run] = []


def _tail(path: str, n: int) -> List[str]:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    # 滤掉 httpx/openai 的噪声行，留业务日志
    lines = [ln for ln in lines if " - httpx - " not in ln and " - openai" not in ln]
    return lines[-n:]


def _run_state_path(run_id: str) -> Path:
    return RUN_DIR / f"{run_id}.state.json"


def _run_to_state(run: _Run) -> dict:
    return {
        "run_id": run.run_id,
        "task": run.task,
        "line_id": run.line_id,
        "jobs": run.jobs,
        "log_path": run.log_path,
        "pid": getattr(run.proc, "pid", None),
        "status": run.status,
        "started": run.started,
    }


def _write_run_state(state: dict) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = _run_state_path(str(state.get("run_id") or ""))
    tmp = path.with_suffix(".state.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _pid_is_running(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid_int)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            try:
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid_int, 0)
        return True
    except OSError:
        return False


def _infer_finished_status_from_log(log_path: str) -> str:
    lines = _tail(log_path, 200)
    if any("用户停止了当前任务" in ln for ln in lines):
        return "cancelled"
    for ln in reversed(lines):
        m = re.search(r"done\. total=\d+ success=\d+ failed=(\d+)", ln)
        if m:
            return "done" if int(m.group(1)) == 0 else "failed"
    if any(" failed:" in ln.lower() or " - error - " in ln.lower() for ln in lines):
        return "failed"
    return "failed"


def _read_run_states() -> List[dict]:
    states: List[dict] = []
    for path in RUN_DIR.glob("*.state.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict) or not state.get("run_id"):
            continue
        if state.get("status") == "running" and not _pid_is_running(state.get("pid")):
            state["status"] = _infer_finished_status_from_log(str(state.get("log_path") or ""))
            _write_run_state(state)
        states.append(state)
    return sorted(states, key=lambda s: float(s.get("started") or 0), reverse=True)


def _active_state() -> Optional[dict]:
    for state in _read_run_states():
        if state.get("status") == "running":
            return state
    return None


def _serialize_state(state: dict, tail: int = 60) -> dict:
    log_path = str(state.get("log_path") or "")
    log_lines = _tail(log_path, tail)
    started = float(state.get("started") or 0)
    return {
        "run_id": state.get("run_id"),
        "task": state.get("task") or "",
        "line_id": state.get("line_id") or "",
        "jobs": list(state.get("jobs") or []),
        "status": state.get("status") or "failed",
        "started": time.strftime("%H:%M:%S", time.localtime(started)) if started else "",
        "summary": _summarize_log(log_lines, str(state.get("status") or "failed"), len(state.get("jobs") or [])),
        "log": log_lines,
    }


def _summarize_log(lines: List[str], status: str, total_jobs: int) -> dict:
    """从原始日志尾部抽一个业务摘要；原始日志仍原样返回供排查。"""
    generated = sum(1 for ln in lines if " generated:" in ln)
    drafted = sum(1 for ln in lines if " POST wechat/" in ln)
    blocked = sum(1 for ln in lines if "BLOCKED" in ln or " publish_blocked" in ln)
    missing_figures = 0
    for ln in lines:
        if m := re.search(r"配图：(\d+) 个图片占位符未配到图片", ln):
            missing_figures += int(m.group(1))
            continue
        if m := re.search(r"剩\s*(\d+)\s*个占位符未配", ln):
            missing_figures += int(m.group(1))
    error_lines = [ln for ln in lines if " failed:" in ln.lower() or " - error - " in ln.lower()]
    failed_jobs = {
        m.group(1)
        for ln in error_lines
        if (m := re.search(r"\[([^\]]+)\]", ln))
    }
    failed = len(failed_jobs) if failed_jobs else len(error_lines)
    last_problem = ""
    for ln in reversed(lines):
        low = ln.lower()
        if " failed:" in low or " - error - " in low or "blocked" in low:
            last_problem = ln
            break

    invalid_ip = ""
    for ln in error_lines:
        m = re.search(r"invalid ip ([0-9a-fA-F:.]+)", ln)
        if m and "not in whitelist" in ln.lower():
            invalid_ip = m.group(1)
            break

    if status == "cancelled":
        message = "任务已停止，已保留已有日志"
    elif invalid_ip:
        message = f"公众号接口拒绝服务器 IP：{invalid_ip} 未在白名单，请先在公众号后台加入 IP 白名单"
    elif failed:
        message = f"发现 {failed} 个失败，建议先看摘要下方最后一条错误"
    elif blocked:
        message = f"{blocked} 篇被质量闸拦下，已保留文章供预览"
    elif status == "done":
        if drafted:
            message = f"运行完成：{drafted}/{total_jobs} 篇已提交到公众号草稿箱"
        else:
            message = f"运行完成：{generated}/{total_jobs} 篇生成日志已记录；未看到公众号草稿提交日志"
    else:
        message = f"运行中：{generated}/{total_jobs} 篇已生成，{drafted}/{total_jobs} 篇已提交草稿"

    if missing_figures and not (failed or blocked or invalid_ip):
        message = f"{message}；未配图 {missing_figures} 张"

    return {
        "total": int(total_jobs or 0),
        "generated": generated,
        "drafted": drafted,
        "blocked": blocked,
        "failed": failed,
        "missing_figures": missing_figures,
        "message": message,
        "last_problem": last_problem,
    }


def _reap() -> Optional[_Run]:
    """检查当前子进程是否结束；结束则归档到 history，返回仍在跑的 run（或 None）。"""
    global _current
    if _current and _current.proc is not None and _current.proc.poll() is not None:
        rc = _current.proc.returncode
        _current.status = "done" if rc == 0 else "failed"
        _write_run_state(_run_to_state(_current))
        logger.info("panel run %s finished rc=%s", _current.run_id, rc)
        _history.insert(0, _current)
        del _history[6:]
        _current = None
    elif _current:
        _write_run_state(_run_to_state(_current))
    return _current


def cancel_run() -> dict:
    """停止当前面板任务；用于业务误点或发现素材选错时快速止损。"""
    global _current
    with _lock:
        cur = _reap()
        if cur is None:
            return {"ok": False, "error": "没有正在运行的任务"}
        try:
            if cur.proc is not None and cur.proc.poll() is None:
                cur.proc.terminate()
        except Exception as exc:  # pragma: no cover - 取决于平台/进程状态
            return {"ok": False, "error": f"停止失败：{exc}"}
        cur.status = "cancelled"
        try:
            with open(cur.log_path, "a", encoding="utf-8") as fh:
                fh.write("\n[panel] 用户停止了当前任务\n")
        except OSError:
            pass
        _write_run_state(_run_to_state(cur))
        logger.info("panel run %s cancelled", cur.run_id)
        _history.insert(0, cur)
        del _history[6:]
        _current = None
        return {"ok": True, "run_id": cur.run_id}


def start_run(line_id: str, pdfs: List[str]) -> dict:
    """为选中的 PDF 起一个后台流水线子进程（generate + distribute）。单活跃任务。"""
    global _current
    with _lock:
        if _reap() is not None or _active_state() is not None:
            return {"ok": False, "error": "已有任务在跑，等它结束再开"}
        line_id = (line_id or "").strip()
        pdfs = [p for p in (pdfs or []) if str(p).strip()]
        if not pdfs:
            return {"ok": False, "error": "没有选中任何 PDF"}
        try:
            line = load_line_by_id(str(LINES_DIR), line_id)
        except LineLoadError as exc:
            return {"ok": False, "error": f"线配置无效：{exc}"}

        run_id = uuid.uuid4().hex[:8]
        task = f"panel-{line_id}-{time.strftime('%Y%m%d-%H%M%S')}"
        jobs = []
        for rel in pdfs:
            stem = Path(rel).stem
            _unmark_operator_pending(line_id, rel)
            jobs.append({
                "job_id": _job_id_from_pdf(stem), "line": line_id, "pdf": rel,
                "template": line.template, "product": line.product,
                "extra": {"max_pages": _DEFAULT_MAX_PAGES},
            })
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        jobs_path = RUN_DIR / f"{run_id}.jobs.yaml"
        jobs_path.write_text(
            yaml.safe_dump({"jobs": jobs}, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )
        log_path = RUN_DIR / f"{run_id}.log"
        log_fh = open(log_path, "w", encoding="utf-8")  # 子进程持有，进程结束自动释放
        proc = subprocess.Popen(
            [sys.executable, "batch_processor.py", "--jobs", str(jobs_path),
             "--stage", "all", "--task", task],
            cwd=str(PROJECT_ROOT), stdout=log_fh, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
        _current = _Run(
            run_id=run_id, task=task, line_id=line_id,
            jobs=[j["job_id"] for j in jobs], log_path=str(log_path), proc=proc,
        )
        _write_run_state(_run_to_state(_current))
        logger.info("panel run %s started: line=%s jobs=%s", run_id, line_id, _current.jobs)
        return {"ok": True, "run_id": run_id, "jobs": _current.jobs}


def runs_status() -> dict:
    with _lock:
        _reap()
        states = _read_run_states()
        active = next((s for s in states if s.get("status") == "running"), None)
        history = [s for s in states if s.get("status") != "running"][:6]
        return {
            "busy": active is not None,
            "current": _serialize_state(active) if active else None,
            "history": [_serialize_state(s, tail=12) for s in history],
        }
