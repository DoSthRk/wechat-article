"""Subprocess entrypoint for one translation or CMS publication batch."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from db.database import get_db_manager
from utils.blog_pipeline import BlogPipelineError, BlogWorkflow


def write_state(path: Path, state: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("translate", "publish"), required=True)
    parser.add_argument("--selections", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--lock", required=True)
    args = parser.parse_args()
    state_path, lock_path = Path(args.state), Path(args.lock)
    items = json.loads(Path(args.selections).read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pid"] = os.getpid()
    write_state(state_path, state)
    workflow = BlogWorkflow(get_db_manager())
    try:
        for item in items:
            state["current"] = item
            write_state(state_path, state)
            try:
                if args.stage == "translate":
                    workflow.translate(item["job_id"], item["lang"])
                else:
                    workflow.publish(item["job_id"], item["lang"])
                state["completed"] += 1
                print(f"[workflow] {args.stage} {item['job_id']} {item['lang']} ok", flush=True)
            except BlogPipelineError as exc:
                state["failed"] += 1
                state["errors"].append(f"{item['job_id']} / {item['lang']}: {exc}")
                print(f"[workflow] {args.stage} {item['job_id']} {item['lang']} failed: {exc}", flush=True)
            write_state(state_path, state)
        state["status"] = "done" if not state["failed"] else "failed"
        return_code = 0 if not state["failed"] else 1
    except Exception as exc:
        state["status"] = "failed"
        state["errors"].append(str(exc) or exc.__class__.__name__)
        return_code = 1
    finally:
        state["current"] = None
        state["finished_at"] = time.time()
        write_state(state_path, state)
        lock_path.unlink(missing_ok=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
