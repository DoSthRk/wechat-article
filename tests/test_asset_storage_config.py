import hashlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from config import asset_public_base_url, asset_storage_mode
from utils.blog_pipeline import BlogPipelineError, OssImageStore, create_blog_image_store
from utils.source_pdf_store import OssSourcePdfStore, SourcePdfError, PublishedSourcePdf


class AssetStorageConfigTests(unittest.TestCase):
    def test_common_backend_and_domain_override_legacy_origin_settings(self):
        env = {"ARTICLE_ASSET_STORAGE": "oss", "ARTICLE_ASSET_PUBLIC_BASE_URL": "https://img.genemedi.cn/",
               "BLOG_IMAGE_STORAGE": "ssh", "SOURCE_PDF_STORAGE": "ssh"}
        for legacy in ["BLOG_IMAGE_STORAGE", "SOURCE_PDF_STORAGE"]:
            self.assertEqual(asset_storage_mode(legacy, env), "oss")
        self.assertEqual(asset_public_base_url("https://www.genemedi.net/uploads/papers", env),
                         "https://img.genemedi.cn")
        with patch.dict(os.environ, env, clear=True), patch.object(OssImageStore, "from_env") as oss:
            create_blog_image_store()
        oss.assert_called_once()

    def test_legacy_explicit_ssh_rollback_remains_available(self):
        env = {"ARTICLE_ASSET_STORAGE": "ssh", "BLOG_IMAGE_STORAGE": "oss"}
        with patch.dict(os.environ, env, clear=True), patch("utils.blog_pipeline.SshImageStore.from_env") as ssh:
            create_blog_image_store()
        ssh.assert_called_once()

    def test_bad_asset_origins_fail_before_any_upload(self):
        for base in ["http://img.example", "https://img.example/uploads", "https://user:secret@img.example",
                     "https://img.example?token=secret", "https://img.example#part"]:
            with self.subTest(base=base):
                env = {"ARTICLE_ASSET_PUBLIC_BASE_URL": base}
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(BlogPipelineError):
                        OssImageStore.from_env()
                with self.assertRaises(SourcePdfError):
                    OssSourcePdfStore.from_env(env)

    def test_image_upload_sets_immutable_cache_and_content_identity(self):
        bucket = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "figure.jpg"
            path.write_bytes(b"approved-image-bytes")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch("utils.blog_pipeline.verify_public_image", side_effect=lambda url, **kw: url) as verify:
                result = OssImageStore(bucket, "article-assets/blog", "https://img.genemedi.cn").upload(str(path))
            headers = bucket.put_object_from_file.call_args.kwargs["headers"]
            self.assertEqual(headers["x-oss-meta-sha256"], digest)
            self.assertEqual(headers["Content-Type"], "image/jpeg")
            self.assertIn("immutable", headers["Cache-Control"])
            self.assertEqual(result, f"https://img.genemedi.cn/article-assets/blog/{digest}.jpg")
            verify.assert_called_once_with(result, expected_path=path)

    def test_cached_website_pdf_is_reuploaded_after_asset_origin_switch(self):
        from batch_processor import _ensure_source_pdf_published

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.pdf"
            path.write_bytes(b"%PDF-1.7\nsource\n%%EOF\n")
            digest, size = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size
            db = Mock()
            db.get_job.return_value = SimpleNamespace(
                source_pdf_url="https://www.genemedi.net/uploads/papers/old.pdf",
                source_pdf_sha256=digest, source_pdf_size=size)
            new_url = f"https://img.genemedi.cn/article-assets/source-pdfs/{digest[:2]}/{digest}.pdf"
            store = Mock()
            store.upload.return_value = PublishedSourcePdf(new_url, digest, size)
            env = {"ARTICLE_ASSET_STORAGE": "oss", "ARTICLE_ASSET_PUBLIC_BASE_URL": "https://img.genemedi.cn"}
            with patch.dict(os.environ, env, clear=True), patch("batch_processor.create_source_pdf_store", return_value=store):
                result = _ensure_source_pdf_published(db, 7, SimpleNamespace(pdf=str(path), job_id="paper"))
            self.assertEqual(result, new_url)
            store.upload.assert_called_once_with(str(path))
            db.update_job_source_pdf.assert_called_once_with(7, url=new_url, sha256=digest, size=size)


if __name__ == "__main__":
    unittest.main()
