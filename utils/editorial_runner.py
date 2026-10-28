"""持久化队列的启动器。读取页面不会启动任务；写入和服务启动负责唤醒。"""
from pathlib import Path
import fcntl
import logging
import os
import subprocess
import sys
import threading
import time

_watchdog = None


def enabled() -> bool:
    default = 'true' if (Path(__file__).resolve().parent.parent / 'deploy' / 'editorial-enabled').is_file() else 'false'
    return os.getenv('EDITORIAL_WORKBENCH_ENABLED', default).lower() == 'true'


def kick(kind: str) -> None:
    if kind not in {'prepare', 'publish'}: raise ValueError('invalid editorial worker')
    root = Path(__file__).resolve().parent.parent
    from utils.editorial_store import Store
    store = Store()
    with (store.root / f'{kind}.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    with (store.root / f'{kind}.log').open('ab') as log:
        subprocess.Popen([sys.executable, str(root / 'editorial_worker.py'), kind], cwd=root,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)


def start_watchdog():
    """服务存活期间恢复退出的队列进程；状态 GET 无启动副作用。"""
    global _watchdog
    if _watchdog and _watchdog.is_alive(): return
    def watch():
        while True:
            for kind in ('prepare', 'publish'):
                try: kick(kind)
                except Exception: logging.getLogger(__name__).exception('Unable to start editorial %s worker', kind)
            time.sleep(30)
    _watchdog = threading.Thread(target=watch, name='editorial-watchdog', daemon=True)
    _watchdog.start()
