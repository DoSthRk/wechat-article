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
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    overwrite = dest.exists()
    dest.write_bytes(data)
    logger.info(
        "panel upload: line=%s file=%s bytes=%d overwrite=%s", line_id, name, len(data), overwrite,
    )
    try:
        rel = str(dest.relative_to(PROJECT_ROOT)).replace("\\", "/")  # 生产：项目相对路径
    except ValueError:
        rel = dest.as_posix()  # 测试场景：PDFS_DIR 被指到项目外的临时目录
    return {
        "ok": True, "name": name, "overwrite": overwrite,
        "pdf": rel, "job_id": _job_id_from_pdf(dest.stem),
    }


def _line_ids() -> List[str]:
    return sorted(p.stem for p in LINES_DIR.glob("*.yaml")) if LINES_DIR.is_dir() else []


def _norm_pdf_key(path: str) -> str:
    """把 PDF 路径统一成「项目内相对 posix 小写」做匹配键（吃绝对/相对、正反斜杠、大小写）。"""
    pp = Path(path)
    try:
        return pp.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix().lower()
    except (ValueError, OSError):
        return pp.name.lower()  # 项目外 / 解析失败 → 退化到文件名


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
        files = []
        for p in pdfs:
            info = by_pdf.get(_norm_pdf_key(str(p)))
            dists = info.get("distributions", []) if info else []
            files.append({
                "pdf": str(p.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "name": p.name,
                # 绑定到则用文章真实 job_id（预览/状态用）；否则用文件名推导值（仅供新跑）
                "job_id": (info.get("job_id") if info else _job_id_from_pdf(p.stem)),
                "bound": bool(info),
                "has_article": bool(info and info.get("title")),
                "title": (info.get("title") if info else None),
                "published": any(x.get("publish_status") == "published" for x in dists),
                "blocked": bool(info.get("publish_blocked")) if info else False,
            })
        out.append({
            "line_id": line_id,
            "name": line.name,
            "account": str((line.extra or {}).get("wechat_account") or ""),
            "folder": folder,
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
    status: str = "running"  # running | done | failed
    started: float = field(default_factory=time.time)

    def serialize(self, tail: int = 60) -> dict:
        return {
            "run_id": self.run_id, "task": self.task, "line_id": self.line_id,
            "jobs": self.jobs, "status": self.status,
            "started": time.strftime("%H:%M:%S", time.localtime(self.started)),
            "log": _tail(self.log_path, tail),
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


def _reap() -> Optional[_Run]:
    """检查当前子进程是否结束；结束则归档到 history，返回仍在跑的 run（或 None）。"""
    global _current
    if _current and _current.proc is not None and _current.proc.poll() is not None:
        rc = _current.proc.returncode
        _current.status = "done" if rc == 0 else "failed"
        logger.info("panel run %s finished rc=%s", _current.run_id, rc)
        _history.insert(0, _current)
        del _history[6:]
        _current = None
    return _current


def start_run(line_id: str, pdfs: List[str]) -> dict:
    """为选中的 PDF 起一个后台流水线子进程（generate + distribute）。单活跃任务。"""
    global _current
    with _lock:
        if _reap() is not None:
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
        logger.info("panel run %s started: line=%s jobs=%s", run_id, line_id, _current.jobs)
        return {"ok": True, "run_id": run_id, "jobs": _current.jobs}


def runs_status() -> dict:
    with _lock:
        cur = _reap()
        return {
            "busy": cur is not None,
            "current": cur.serialize() if cur else None,
            "history": [r.serialize(tail=12) for r in _history],
        }
