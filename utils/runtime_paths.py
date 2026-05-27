from pathlib import Path
from typing import Dict


def resolve_runtime_dir(base_dir: str) -> Path:
    base_path = Path(base_dir).resolve()
    if base_path.parent.name == "releases":
        return base_path.parent.parent / "shared" / "runtime"
    return base_path / "runtime"


def build_runtime_files(base_dir: str) -> Dict[str, Path]:
    runtime_dir = resolve_runtime_dir(base_dir)
    return {
        "dir": runtime_dir,
        "state": runtime_dir / "batch_state.json",
        "pid": runtime_dir / "batch_processor.pid",
        "control": runtime_dir / "batch_control.json",
    }
