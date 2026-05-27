"""通用受控阶段运行器。

在后台线程里逐项处理工作清单，支持 暂停 / 继续 / 停止 与进度查询。
线程式（非进程），适用于 I/O 密集的翻译 / 发布段。一个 StageRunner 实例
管理一段流水线，同一时刻只跑一个批次。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.logger import setup_logger

logger = setup_logger("stage_runner")

#: worker 签名：接收一个工作项，返回 (是否成功, 说明文字)
Worker = Callable[[Any], Tuple[bool, str]]


class StageRunner:
    """单段受控运行器。"""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    DONE = "done"

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()  # 默认非暂停
        self._reset_progress()

    def _reset_progress(self) -> None:
        self._status = self.IDLE
        self._total = 0
        self._index = 0
        self._succeeded = 0
        self._failed = 0
        self._current_item: Any = None
        self._started_at: Optional[float] = None
        self._finished_at: Optional[float] = None
        self._last_error = ""

    def is_active(self) -> bool:
        """是否有批次在跑（running / paused / stopping）。"""
        with self._lock:
            return self._status in (self.RUNNING, self.PAUSED, self.STOPPING)

    def start(self, items: List[Any], worker: Worker, *, interval: float = 0.0) -> bool:
        """启动一个批次。

        Args:
            items: 工作项列表。
            worker: ``callable(item) -> (ok, info)``。
            interval: 每项之间的间隔秒数（限速滴灌；可被 stop 打断）。

        Returns:
            ``False`` 表示已有批次在跑、本次未启动。
        """
        with self._lock:
            if self._status in (self.RUNNING, self.PAUSED, self.STOPPING):
                return False
            self._reset_progress()
            self._status = self.RUNNING
            self._total = len(items)
            self._started_at = time.time()
        self._stop_event.clear()
        self._resume_event.set()
        self._thread = threading.Thread(
            target=self._run, args=(list(items), worker, interval), daemon=True
        )
        self._thread.start()
        return True

    def _run(self, items: List[Any], worker: Worker, interval: float) -> None:
        for idx, item in enumerate(items, start=1):
            if self._stop_event.is_set():
                break
            # 暂停点：阻塞直到 resume 或 stop
            self._resume_event.wait()
            if self._stop_event.is_set():
                break
            with self._lock:
                self._index = idx
                self._current_item = item
            try:
                ok, info = worker(item)
            except Exception as exc:  # worker 不应让整批崩掉
                ok, info = False, str(exc)
                logger.warning("%s worker raised on item %r: %s", self.name, item, exc)
            with self._lock:
                if ok:
                    self._succeeded += 1
                else:
                    self._failed += 1
                    self._last_error = info or ""
            # 限速：除最后一项外，等待（可被 stop 打断）
            if interval > 0 and idx < len(items):
                self._stop_event.wait(timeout=interval)

        with self._lock:
            self._current_item = None
            self._finished_at = time.time()
            self._status = self.IDLE if self._stop_event.is_set() else self.DONE

    def pause(self) -> bool:
        with self._lock:
            if self._status != self.RUNNING:
                return False
            self._status = self.PAUSED
        self._resume_event.clear()
        return True

    def resume(self) -> bool:
        with self._lock:
            if self._status != self.PAUSED:
                return False
            self._status = self.RUNNING
        self._resume_event.set()
        return True

    def stop(self) -> bool:
        with self._lock:
            if self._status not in (self.RUNNING, self.PAUSED):
                return False
            self._status = self.STOPPING
        self._stop_event.set()
        self._resume_event.set()  # 解除暂停阻塞，让线程看到 stop
        return True

    def join(self, timeout: Optional[float] = None) -> None:
        """等待当前批次线程结束（主要供测试 / 优雅关停用）。"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def snapshot(self) -> Dict[str, Any]:
        """当前进度快照（线程安全），供 dashboard 读取。"""
        with self._lock:
            percent = round(self._index / self._total * 100, 1) if self._total else 0.0
            return {
                "name": self.name,
                "status": self._status,
                "total": self._total,
                "index": self._index,
                "succeeded": self._succeeded,
                "failed": self._failed,
                "progress_percent": percent,
                "current_item": self._current_item,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "last_error": self._last_error,
            }
