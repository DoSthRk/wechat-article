"""No network: durable outbox + real BlogWorkflow with fake translator/CMS."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from db.database import AutoBlogTask, BLOG_TARGET_LANGS, BlogNotification, DatabaseManager, JobStatus
from utils import auto_blog, blog_task_lock, blog_notifications
from utils.blog_pipeline import BlogWorkflow
from utils.translator import TranslationResult


class AutoBlogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = DatabaseManager(f"sqlite:///{self.base / 'test.db'}")
        self.real_kick = auto_blog.kick
        self.patches = [
            patch.object(auto_blog, "RUN_DIR", self.base / "runs"),
            patch.object(blog_task_lock, "LOCK_DIR", self.base / "locks"),
            patch.object(auto_blog, "kick"),
            patch.dict(os.environ, {"AUTO_BLOG_ENABLED": "true", "GENEMEDI_BLOG_CHINESE_LANGCODE": "zh-hans",
                                    "FEISHU_BLOG_RECIPIENT_OPEN_ID": "", "FEISHU_BLOG_RECIPIENT_EMAIL": "zxy@genemedi.net",
                                    "FEISHU_BLOG_COPY_RECIPIENT_OPEN_ID": "", "FEISHU_BLOG_COPY_RECIPIENT_EMAIL": "",
                                    "FEISHU_RECIPIENT_EDM_OPS_OPEN_ID": ""}),
        ]
        for item in self.patches:
            item.start()
        self.kick_mock = auto_blog.kick
        self.pk = self.make_job("paper-1")
        self.translator = Mock(side_effect=lambda _text, lang: TranslationResult(
            True, lang, f"# Title {lang}\n\nTranslated body", model="fake",
        ))
        self.cms = Mock()
        self.cms.create.return_value = {"uuid": "uuid-1", "node_id": "1"}
        self.workflow = BlogWorkflow(self.db, translator=self.translator,
            blog_client_factory=lambda: self.cms, blog_url_verifier=lambda url: url,
            source_pdf_publisher=lambda *_args: "https://example.com/paper.pdf")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.db.engine.dispose()
        self.temp.cleanup()

    def make_job(self, job_id, task_name="t"):
        task = self.db.get_or_create_task(task_name)
        pk = self.db.upsert_job(task.id, job_id, pdf_path="p", template_id="t", product_id="",
                                status=JobStatus.PUBLISHED).id
        content = self.base / f"article-{pk}"
        content.mkdir()
        (content / "article.md").write_text("# 中文标题\n\n中文正文", encoding="utf-8")
        self.db.upsert_article(pk, content_dir=str(content), publish_blocked=False)
        self.db.ensure_blog_versions(pk, str(content))
        return pk

    def rows(self):
        with self.db.get_session() as session:
            rows = session.query(AutoBlogTask).order_by(AutoBlogTask.id).all()
            for row in rows:
                session.expunge(row)
            return rows

    def run_worker(self):
        auto_blog.drain(self.db, self.workflow)

    def test_no_historical_backfill(self):
        self.run_worker()
        self.assertEqual(self.rows(), [])
        self.cms.create.assert_not_called()

    def test_duplicate_enqueue_drains_four_languages_and_skips_repeat(self):
        self.assertEqual(auto_blog.enqueue(self.db, self.pk, "paper-1", "aav"), 4)
        self.assertEqual(auto_blog.enqueue(self.db, self.pk, "paper-1", "aav"), 0)
        self.run_worker()
        self.assertEqual(self.translator.call_count, 4)
        self.assertEqual(self.cms.create.call_count, 4)
        self.assertTrue(all(r.status == "done" for r in self.rows()))
        auto_blog.enqueue(self.db, self.pk, "paper-1", "aav")
        self.run_worker()
        self.assertEqual(self.cms.create.call_count, 4)
        for lang in BLOG_TARGET_LANGS:
            self.assertEqual(self.db.get_distribution(self.pk, "blog", account="genemedi", lang=lang).publish_status, "published")

    def test_existing_translation_and_publication_are_reused(self):
        self.workflow.translate("paper-1", "en")
        self.workflow.translate("paper-1", "ja")
        self.workflow.publish("paper-1", "ja")
        self.translator.reset_mock()
        self.cms.reset_mock()
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.run_worker()
        self.assertEqual(self.translator.call_count, 2)
        self.assertEqual(self.cms.create.call_count, 3)

    def test_language_failure_isolated_and_explicit_reenqueue_retries(self):
        original = self.translator.side_effect
        self.translator.side_effect = lambda text, lang: TranslationResult(False, lang, "", error="failed") if lang == "en" else original(text, lang)
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.run_worker()
        self.assertEqual([r.status for r in self.rows()], ["failed", "done", "done", "done"])
        self.run_worker()
        self.assertEqual(self.translator.call_count, 4)  # no endless retry/cost loop
        self.translator.side_effect = original
        self.assertEqual(auto_blog.enqueue(self.db, self.pk, "paper-1"), 1)
        self.run_worker()
        self.assertTrue(all(r.status == "done" for r in self.rows()))

    def test_interrupted_task_and_second_article_resume_after_db_reopen(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        auto_blog._update(self.db, self.rows()[0].id, status="running", phase="translate")
        pk2 = self.make_job("paper-2")
        auto_blog.enqueue(self.db, pk2, "paper-2")
        url = self.db.engine.url.render_as_string(hide_password=False)
        self.db.engine.dispose()
        self.db = DatabaseManager(url)
        self.workflow.db = self.db
        self.run_worker()
        self.assertEqual(len(self.rows()), 8)
        self.assertTrue(all(r.status == "done" for r in self.rows()))

    def test_worker_busy_preserves_queue(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        with auto_blog.worker_lock() as acquired:
            self.assertTrue(acquired)
            self.run_worker()
        self.assertTrue(all(r.status == "queued" for r in self.rows()))
        self.run_worker()
        self.assertTrue(all(r.status == "done" for r in self.rows()))

    def test_holds_and_disable_never_enqueue(self):
        self.assertEqual(auto_blog.enqueue(self.db, self.pk, "免疫客文章-4-3"), 0)
        self.assertEqual(auto_blog.enqueue(self.db, self.pk, "免疫客文章-4-4"), 0)
        with patch.dict(os.environ, {"AUTO_BLOG_ENABLED": "false"}):
            self.assertEqual(auto_blog.enqueue(self.db, self.pk, "paper-1"), 0)
        self.assertEqual(self.rows(), [])

    def test_source_change_cancels_old_revision(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        source = Path(self.db.get_article_version(self.pk, "zh").content_path)
        source.write_text("# 新文章", encoding="utf-8")
        self.run_worker()
        self.assertTrue(all(r.status == "cancelled" for r in self.rows()))
        self.cms.create.assert_not_called()

    def test_pins_job_revision_even_if_newer_task_has_same_job_id(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        newest = self.make_job("paper-1", "new-task")
        self.run_worker()
        self.assertEqual(self.db.get_article_version(self.pk, "en").translation_status, "translated")
        self.assertEqual(self.db.get_article_version(newest, "en").translation_status, "pending")

    def test_quality_gate_and_image_gate_still_block_publication(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.db.upsert_article(self.pk, publish_blocked=True)
        self.run_worker()
        self.assertTrue(all(r.status == "failed" for r in self.rows()))
        self.cms.create.assert_not_called()
        self.db.upsert_article(self.pk, publish_blocked=False)
        source = Path(self.db.get_article_version(self.pk, "zh").content_path)
        source.write_text("# 中文标题\n\n[图片:Figure 1 结果]", encoding="utf-8")
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.run_worker()
        self.cms.create.assert_not_called()

    def test_status_filters_business_line(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1", "aav")
        pk2 = self.make_job("paper-2")
        auto_blog.enqueue(self.db, pk2, "paper-2", "solidex")
        result = auto_blog.status(self.db, {"aav"})
        self.assertEqual(result["total"], 4)
        self.assertTrue(all(r["job_id"] == "paper-1" for r in result["items"]))

    def test_same_content_regeneration_resets_completed_queue(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.run_worker()
        self.db.ensure_blog_versions(self.pk, self.db.get_article(self.pk).content_dir, reset=True)
        self.assertEqual(auto_blog.enqueue(self.db, self.pk, "paper-1"), 4)
        self.run_worker()
        self.assertEqual(self.translator.call_count, 8)
        self.assertEqual(self.cms.update.call_count, 4)

    def test_source_change_during_translation_stops_cms_write(self):
        source = Path(self.db.get_article_version(self.pk, "zh").content_path)
        original = self.translator.side_effect
        def change_source(text, lang):
            source.write_text("# Changed source", encoding="utf-8")
            return original(text, lang)
        self.translator.side_effect = change_source
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.run_worker()
        self.assertEqual(self.rows()[0].status, "failed")
        self.cms.create.assert_not_called()

    def test_idle_exit_rechecks_queue_only_after_releasing_worker_lock(self):
        def check_lock(_db):
            with auto_blog.worker_lock() as available:
                self.assertTrue(available)
        self.kick_mock.side_effect = check_lock
        self.run_worker()
        self.kick_mock.assert_called_once_with(self.db)

    def test_launch_failure_keeps_durable_tasks_for_later_recovery(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1", launch=False)
        with patch.object(auto_blog.subprocess, "Popen", side_effect=OSError("launch failed")):
            with self.assertRaises(OSError):
                self.real_kick(self.db)
        self.assertTrue(all(r.status == "queued" for r in self.rows()))
        with patch.object(auto_blog.subprocess, "Popen") as popen:
            self.real_kick(self.db)
        command = popen.call_args.args[0]
        self.assertIn("auto_blog_worker.py", command)
        self.assertEqual(popen.call_args.kwargs["env"]["DATABASE_URL"], str(self.db.engine.url))

    def notice(self):
        with self.db.get_session() as session:
            row = session.query(BlogNotification).first()
            session.expunge(row)
            return row

    def publish_all(self):
        self.workflow.publish("paper-1", "zh")
        for lang in BLOG_TARGET_LANGS:
            self.workflow.translate("paper-1", lang)
            self.workflow.publish("paper-1", lang)

    def test_completion_notification_has_all_five_links_and_is_sent_once(self):
        self.workflow.publish("paper-1", "zh")
        auto_blog.enqueue(self.db, self.pk, "paper-1", "aav")
        with patch.object(blog_notifications, "send_notice", return_value="om_receipt") as sender:
            self.run_worker()
            auto_blog.enqueue(self.db, self.pk, "paper-1", "aav")
            self.run_worker()
        sender.assert_called_once()
        row, text = sender.call_args.args
        self.assertEqual(row.recipient_id, "zxy@genemedi.net")
        self.assertEqual(row.recipient_type, "email")
        self.assertTrue(text.startswith("Blog 已发布"))
        for lang in ("zh", *BLOG_TARGET_LANGS):
            dist = self.db.get_distribution(self.pk, "blog", account="genemedi", lang=lang)
            self.assertIn(dist.external_url, text)
        self.assertEqual(self.notice().status, "sent")
        self.assertEqual(self.notice().message_id, "om_receipt")
        self.assertFalse(blog_notifications.pending(self.db))

    def test_incomplete_or_legacy_publication_does_not_send(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        with patch.object(blog_notifications, "send_notice") as sender:
            self.run_worker()  # Chinese Blog still pending
            sender.assert_not_called()
            self.workflow.publish("paper-1", "zh")
            self.db.upsert_distribution(self.pk, "blog", account="genemedi", lang="en",
                publish_status="published", external_url="https://en.genemedi.com/blog/old")
            blog_notifications.deliver_ready(self.db)
        sender.assert_not_called()
        self.assertEqual(self.notice().status, "waiting")

    def test_manual_repair_of_final_language_makes_notification_ready(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.workflow.publish("paper-1", "zh")
        original = self.translator.side_effect
        self.translator.side_effect = lambda text, lang: TranslationResult(False, lang, "", error="failed") if lang == "en" else original(text, lang)
        with patch.object(blog_notifications, "send_notice", return_value="om_repaired") as sender:
            self.run_worker()
            sender.assert_not_called()
            self.translator.side_effect = original
            self.workflow.translate("paper-1", "en")
            self.workflow.publish("paper-1", "en")
            self.assertTrue(blog_notifications.pending(self.db))
            self.run_worker()
        sender.assert_called_once()
        self.assertEqual(self.notice().status, "sent")

    def test_notice_failure_does_not_fail_blog_and_can_retry_explicitly(self):
        self.workflow.publish("paper-1", "zh")
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        with patch.object(blog_notifications, "send_notice", side_effect=blog_notifications.NotificationError("permission denied")):
            self.run_worker()
        self.assertTrue(all(r.status == "done" for r in self.rows()))
        self.assertEqual(self.notice().status, "failed")
        self.assertEqual(self.db.get_job(self.pk).status, JobStatus.PUBLISHED)
        self.assertTrue(blog_notifications.retry(self.db, "paper-1")["ok"])
        with patch.object(blog_notifications, "send_notice", return_value="om_retry") as sender:
            self.run_worker()
        sender.assert_called_once()
        self.assertEqual(self.notice().status, "sent")

    def test_transient_retry_preserves_uuid_and_recipient(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.publish_all()
        sender = Mock(side_effect=blog_notifications.NotificationError("timeout", retryable=True))
        self.assertEqual(blog_notifications.deliver_ready(self.db, sender=sender), 30)
        first = self.notice()
        self.assertEqual(first.status, "retry")
        blog_notifications._save(self.db, first, retry_at=datetime.utcnow() - timedelta(seconds=1))
        sender = Mock(return_value="om_retry")
        with patch.dict(os.environ, {"FEISHU_BLOG_RECIPIENT_EMAIL": "different@example.com"}):
            blog_notifications.deliver_ready(self.db, sender=sender)
        actual = sender.call_args.args[0]
        self.assertEqual(actual.delivery_uuid, first.delivery_uuid)
        self.assertEqual(actual.recipient_id, "zxy@genemedi.net")
        self.assertEqual(self.notice().status, "sent")

    def test_expired_ambiguous_delivery_is_not_resent(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.publish_all()
        row = self.notice()
        blog_notifications._save(self.db, row, status="sending", first_attempt_at=datetime.utcnow() - timedelta(hours=2))
        sender = Mock()
        blog_notifications.deliver_ready(self.db, sender=sender)
        sender.assert_not_called()
        self.assertEqual(self.notice().status, "uncertain")
        self.assertFalse(blog_notifications.retry(self.db, "paper-1")["ok"])

    def test_interrupted_delivery_resumes_with_persisted_uuid(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.publish_all()
        row = self.notice()
        blog_notifications._save(self.db, row, status="sending", first_attempt_at=datetime.utcnow(),
                                 recipient_id="zxy@genemedi.net", recipient_type="email")
        with patch.object(blog_notifications, "send_notice", return_value="om_resumed") as sender:
            self.run_worker()
        self.assertEqual(sender.call_args.args[0].delivery_uuid, row.delivery_uuid)
        self.assertEqual(self.notice().status, "sent")

    def test_missing_receipt_and_exhausted_transient_attempts_are_not_success(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.publish_all()
        row = self.notice()
        blog_notifications._save(self.db, row, attempts=4)
        blog_notifications.deliver_ready(self.db, sender=lambda *_args: "")
        self.assertEqual(self.notice().status, "uncertain")
        self.assertIsNone(self.notice().message_id)

    def test_source_change_or_hold_cancels_notice_without_send(self):
        auto_blog.enqueue(self.db, self.pk, "paper-1")
        self.publish_all()
        with patch.dict(os.environ, {"AUTO_BLOG_EXCLUDED_JOBS": "paper-1"}):
            sender = Mock()
            blog_notifications.deliver_ready(self.db, sender=sender)
        sender.assert_not_called()
        self.assertEqual(self.notice().status, "cancelled")

    def test_two_recipients_have_independent_receipts_and_failed_only_retry(self):
        with patch.dict(os.environ, {"FEISHU_BLOG_COPY_RECIPIENT_OPEN_ID": "ou_me"}):
            auto_blog.enqueue(self.db, self.pk, "paper-1")
            self.publish_all()
            def send(row, text):
                if row.recipient_key == "self":
                    raise blog_notifications.NotificationError("permission denied")
                return "om_primary"
            blog_notifications.deliver_ready(self.db, sender=send)
            states = blog_notifications.status(self.db)
            self.assertEqual([n["status"] for n in states], ["sent", "failed"])
            self.assertTrue(blog_notifications.retry(self.db, "paper-1")["ok"])
            sender = Mock(return_value="om_self")
            blog_notifications.deliver_ready(self.db, sender=sender)
            sender.assert_called_once()
            self.assertEqual(sender.call_args.args[0].recipient_key, "self")
            self.assertEqual([n["message_id"] for n in blog_notifications.status(self.db)], ["om_primary", "om_self"])


if __name__ == "__main__":
    unittest.main()
