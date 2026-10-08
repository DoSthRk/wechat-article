"""Send one explicit test per configured recipient, with a persistent receipt journal.

Run on the notification host with --env-file; never export credentials to chat.
The sample JSON contains job_id, title, and [{lang, url}] from published DB rows.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values
from utils import blog_notifications
from utils.blog_urls import public_blog_url


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--journal", required=True)
    args = parser.parse_args()
    for key, value in dotenv_values(args.env_file).items():
        if value is not None and (key in {"FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_RECIPIENT_EDM_OPS_OPEN_ID"}
                                  or key.startswith("FEISHU_BLOG_")):
            os.environ[key] = value
    sample = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    links = {r["lang"]: r["url"] for r in sample["links"]}
    langs = ("zh", "en", "ja", "ko", "ru")
    if any(links.get(lang) != public_blog_url(sample["job_id"], lang) for lang in langs):
        parser.error("sample must contain all five canonical, published Blog URLs")
    keys = blog_notifications.recipient_keys()
    if set(keys) != {"primary", "self"}:
        parser.error("both Zhang Xiaoyan and the existing personal default must be configured")
    text = "\n".join([
        "【测试】Blog 发布通知", "本消息仅验证通知配置，未触发新文章发布。", "",
        f"示例文章：{sample['title']}", f"任务：{sample['job_id']}", "",
        *[f"{blog_notifications.LANG_LABELS[lang]}：{links[lang]}" for lang in langs],
    ])
    journal = Path(args.journal)
    if journal.exists():
        state = json.loads(journal.read_text(encoding="utf-8"))
        if state["text"] != text:
            parser.error("journal belongs to a different test message")
    else:
        state = {"test_id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
                 "text": text, "deliveries": []}
        for key in keys:
            identity, kind = blog_notifications.recipient(key)
            state["deliveries"].append({"recipient_key": key, "recipient_id": identity,
                "recipient_type": kind, "delivery_uuid": str(uuid.uuid4()), "message_id": None,
                "status": "pending"})
        save(journal, state)
    if datetime.now(timezone.utc) - datetime.fromisoformat(state["created_at"]) > timedelta(minutes=55):
        parser.error("test journal exceeds the safe deduplication window; inspect receipts before resending")
    for delivery in state["deliveries"]:
        if delivery["status"] == "sent":
            continue
        delivery["status"] = "sending"
        save(journal, state)
        try:
            delivery["message_id"] = blog_notifications.send_notice(SimpleNamespace(**delivery), text)
            delivery["status"] = "sent"
        except blog_notifications.NotificationError as exc:
            delivery["status"] = "uncertain" if exc.retryable else "failed"
            delivery["error"] = str(exc)
        save(journal, state)
    print(json.dumps({"test_id": state["test_id"], "deliveries": [
        {k: d.get(k) for k in ("recipient_key", "recipient_id", "status", "message_id", "error")}
        for d in state["deliveries"]]}, ensure_ascii=False))
    return 0 if all(d["status"] == "sent" for d in state["deliveries"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
