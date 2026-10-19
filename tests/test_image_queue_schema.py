"""图包队列迁移与回执绑定：只使用临时数据库。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

from sqlalchemy import inspect
from sqlalchemy.schema import CreateTable

from db.database import AutoBlogTask, DatabaseManager


class ImageQueueSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self.temp.name) / 'db.sqlite'}"
        self.db = DatabaseManager(self.url)
        task = self.db.get_or_create_task("image-test")
        self.pk = self.db.upsert_job(task.id, "paper", pdf_path="p.pdf", template_id="t", product_id="p").id

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def legacy_queue(self):
        ddl = str(CreateTable(AutoBlogTask.__table__).compile(dialect=self.db.engine.dialect))
        ddl = "\n".join(line for line in ddl.splitlines() if not line.strip().startswith("image_package_id "))
        ddl = ddl.replace("uq_auto_blog_image_revision", "uq_auto_blog_revision")
        ddl = ddl.replace("UNIQUE (job_pk, lang, source_sha256, image_package_id)", "UNIQUE (job_pk, lang, source_sha256)")
        with self.db.engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE auto_blog_tasks")
            conn.exec_driver_sql(ddl)
            conn.exec_driver_sql("CREATE INDEX ix_auto_blog_status ON auto_blog_tasks(status)")
            conn.exec_driver_sql(
                "INSERT INTO auto_blog_tasks(id,job_pk,job_id,lang,source_sha256,owner_line,status,phase,error) VALUES(7,?,?,?,?,?,?,?,?)",
                (self.pk, "paper", "en", "a" * 64, "aav", "done", "", "legacy receipt"),
            )
        self.db.engine.dispose()

    def test_migration_preserves_old_rows_and_backup_without_approving(self):
        self.legacy_queue()
        self.db = DatabaseManager(self.url)
        backups = list((Path(self.temp.name) / 'image_gate_backups').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(backups[0]) as original:
            self.assertNotIn('image_package_id', {row[1] for row in original.execute('PRAGMA table_info(auto_blog_tasks)')})
            self.assertEqual(original.execute('SELECT status,error FROM auto_blog_tasks WHERE id=7').fetchone(),
                             ('done', 'legacy receipt'))
        with self.db.get_session() as session:
            row = session.get(AutoBlogTask, 7)
            self.assertEqual((row.status, row.error, row.image_package_id), ("done", "legacy receipt", ""))
        with self.db.engine.connect() as conn:
            backup = conn.exec_driver_sql("SELECT status,error FROM auto_blog_tasks_legacy_image_gate WHERE id=7").one()
            self.assertEqual(tuple(backup), ("done", "legacy receipt"))
        self.db.engine.dispose()
        self.db = DatabaseManager(self.url)
        self.assertIn("auto_blog_tasks_legacy_image_gate", inspect(self.db.engine).get_table_names())
        self.assertEqual(list((Path(self.temp.name) / 'image_gate_backups').glob('*.sqlite3')), backups)

    def test_same_markdown_different_package_is_a_distinct_task(self):
        self.legacy_queue()
        self.db = DatabaseManager(self.url)
        with self.db.get_session() as session:
            for package in ("b" * 64, "c" * 64):
                session.add(AutoBlogTask(job_pk=self.pk, job_id="paper", lang="en",
                                        source_sha256="a" * 64, image_package_id=package))
            session.commit()
            self.assertEqual(session.query(AutoBlogTask).count(), 3)

    def test_failed_backup_prevents_any_schema_migration(self):
        self.legacy_queue()
        with patch('db.database.tempfile.NamedTemporaryFile', side_effect=OSError('backup not writable')):
            with self.assertRaisesRegex(OSError, 'backup not writable'):
                DatabaseManager(self.url)
        with sqlite3.connect(Path(self.temp.name) / 'db.sqlite') as original:
            self.assertNotIn('image_package_id', {row[1] for row in original.execute('PRAGMA table_info(auto_blog_tasks)')})
            self.assertEqual(original.execute('SELECT count(*) FROM auto_blog_tasks').fetchone(), (1,))

    def test_parallel_initialization_serializes_schema_change(self):
        self.legacy_queue()
        def initialize(_index):
            db = DatabaseManager(self.url)
            try:
                with db.get_session() as session:
                    return session.query(AutoBlogTask).count()
            finally:
                db.engine.dispose()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(initialize, range(2))), [1, 1])
        self.db = DatabaseManager(self.url)

    def test_distribution_keeps_package_and_verification_evidence(self):
        self.db.upsert_distribution(self.pk, "blog", account="genemedi", lang="en",
                                    image_package_id="b" * 64, image_verification_status="verified",
                                    image_receipt_json='{"images":2}')
        rows, _ = self.db.list_article_overview()
        self.assertEqual(rows[0]["distributions"][0]["image_package_id"], "b" * 64)
        self.assertEqual(rows[0]["distributions"][0]["image_verification_status"], "verified")


if __name__ == "__main__":
    unittest.main()
