"""Print an offline figure-artifact integrity report; never publish or alter caches.

Example snapshot mapping:
  python -B scripts/audit_figure_artifacts.py --jobs-root /snapshot/outputs/jobs \
    --allow-root /snapshot/inputs \
    --path-map /opt/gm-apps/article/shared/outputs/jobs=/snapshot/outputs/jobs \
    --path-map /opt/gm-apps/article/shared/inputs=/snapshot/inputs

Exit 0 means a report was produced, not that articles passed visual review.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.figure_audit import audit_artifacts


def _pair(value):
    key, separator, target = value.partition('=')
    if not separator or not key or not target:
        raise argparse.ArgumentTypeError('expected SOURCE=LOCAL_PATH')
    return key, target


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only offline figure artifact audit; JSON to stdout.')
    parser.add_argument('--jobs-root', required=True)
    parser.add_argument('--allow-root', action='append', default=[], help='Additional explicit local read root.')
    parser.add_argument('--path-map', action='append', type=_pair, default=[], help='Production absolute prefix=local snapshot prefix.')
    parser.add_argument('--pdf', action='append', type=_pair, default=[], help='job_id=local PDF; its directory must be allowed.')
    args = parser.parse_args(argv)
    try:
        report = audit_artifacts(args.jobs_root, allowed_roots=args.allow_root,
                                 path_mappings=args.path_map, pdf_paths=dict(args.pdf))
    except (OSError, ValueError) as exc:
        print(json.dumps({'scope': 'offline_artifact_integrity_only', 'error': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
