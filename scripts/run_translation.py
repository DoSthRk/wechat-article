"""把已生成的中文基准正文翻译成目标语言 —— **仅 blog 链路需要**。

公众号链路用中文原文，不调本脚本（多语言只服务 genemedi {lang}.genemedi.com）。

用法：
    python scripts/run_translation.py --job <job_id>              # 译成全部目标语
    python scripts/run_translation.py --job <job_id> --langs en,ja
    python scripts/run_translation.py --job <job_id> --dry-run    # 只列计划

读  outputs/jobs/{job_id}/article.md
出  outputs/jobs/{job_id}/translations/{lang}.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

from db.database import ARTICLE_CONTENT_DIR
from utils.logger import setup_logger
from utils.translator import SUPPORTED_LANGS, translate_markdown

load_dotenv()
logger = setup_logger("run_translation")


def main() -> int:
    parser = argparse.ArgumentParser(description="翻译基准正文到目标语言（仅 blog 链路）")
    parser.add_argument("--job", required=True, help="job_id（对应 outputs/jobs/{job_id}）")
    parser.add_argument("--langs", default="", help="逗号分隔目标语，如 en,ja；留空=全部")
    parser.add_argument("--dry-run", action="store_true", help="只列计划，不调 LLM")
    args = parser.parse_args()

    langs = [x.strip() for x in args.langs.split(",") if x.strip()] or list(SUPPORTED_LANGS)
    bad = [x for x in langs if x not in SUPPORTED_LANGS]
    if bad:
        logger.error("unsupported langs: %s（支持：%s）", bad, list(SUPPORTED_LANGS))
        return 2

    job_dir = Path(ARTICLE_CONTENT_DIR) / args.job
    src = job_dir / "article.md"
    if not src.exists():
        logger.error("source not found: %s（先 --stage generate 出基准正文）", src)
        return 2
    markdown = src.read_text(encoding="utf-8")
    out_dir = job_dir / "translations"

    summary = {"job": args.job, "langs": langs, "translated": [], "failed": []}
    if args.dry_run:
        summary["dry_run"] = True
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    for lang in langs:
        logger.info("translating job=%s lang=%s", args.job, lang)
        result = translate_markdown(markdown, lang)
        if result.success:
            (out_dir / f"{lang}.md").write_text(result.translated_markdown, encoding="utf-8")
            summary["translated"].append({"lang": lang, "tokens": result.total_tokens})
        else:
            summary["failed"].append({"lang": lang, "error": result.error})

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
