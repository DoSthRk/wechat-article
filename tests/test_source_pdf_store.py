import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.source_pdf_store import (
    OssSourcePdfStore,
    SourcePdfError,
    SshSourcePdfStore,
    inspect_pdf,
)


class _FakeBucket:
    def __init__(self, exists=False):
        self.exists = exists
        self.uploads = []

    def object_exists(self, key):
        self.checked = key
        return self.exists

    def put_object_from_file(self, key, path, headers=None):
        self.uploads.append((key, path, headers or {}))


class SourcePdfStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.pdf = self.base / "原文.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nsource-paper\n%%EOF\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_inspect_pdf_returns_content_identity(self):
        path, digest, size = inspect_pdf(str(self.pdf))
        self.assertEqual(path, self.pdf)
        self.assertEqual(len(digest), 64)
        self.assertEqual(size, self.pdf.stat().st_size)

    def test_inspect_pdf_rejects_non_pdf(self):
        bad = self.base / "bad.pdf"
        bad.write_bytes(b"not a pdf")
        with self.assertRaises(SourcePdfError):
            inspect_pdf(str(bad))

    def test_oss_upload_is_content_addressed_and_sets_pdf_headers(self):
        bucket = _FakeBucket()
        store = OssSourcePdfStore(bucket, "papers", "https://pdf.genemedi.net")
        with patch("utils.source_pdf_store.verify_public_pdf") as verify:
            result = store.upload(str(self.pdf))
        self.assertEqual(result.size, self.pdf.stat().st_size)
        self.assertEqual(len(bucket.uploads), 1)
        key, path, headers = bucket.uploads[0]
        self.assertTrue(key.endswith(f"{result.sha256}.pdf"))
        self.assertEqual(path, str(self.pdf))
        self.assertEqual(headers["Content-Type"], "application/pdf")
        verify.assert_called_once_with(result.url)

    def test_ssh_upload_checks_capacity_and_uses_atomic_remote_name(self):
        key = self.base / "key"
        known = self.base / "known_hosts"
        key.write_text("private", encoding="utf-8")
        known.write_text("host key", encoding="utf-8")
        store = SshSourcePdfStore(
            host="example.com", user="uploader", private_key=str(key), known_hosts=str(known),
            remote_dir="/srv/papers", public_base_url="https://pdf.genemedi.net", min_free_gb=1,
        )
        outputs = iter(["", str(30 * 1024 * 1024), "no", "", ""])

        def fake_run(args, **_kwargs):
            is_scp = args[0] == "scp"
            stdout = "" if is_scp else next(outputs)
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        with patch("utils.source_pdf_store.subprocess.run", side_effect=fake_run) as run:
            with patch("utils.source_pdf_store.verify_public_pdf") as verify:
                result = store.upload(str(self.pdf))
        self.assertTrue(result.url.endswith(f"/{result.sha256[:2]}/{result.sha256}.pdf"))
        self.assertTrue(any(call.args[0][0] == "scp" for call in run.call_args_list))
        self.assertTrue(any(".part" in " ".join(call.args[0]) for call in run.call_args_list))
        verify.assert_called_once_with(result.url, timeout=30.0)

    def test_ssh_upload_stops_below_free_space_threshold(self):
        key = self.base / "key"
        known = self.base / "known_hosts"
        key.write_text("private", encoding="utf-8")
        known.write_text("host key", encoding="utf-8")
        store = SshSourcePdfStore(
            host="example.com", user="uploader", private_key=str(key), known_hosts=str(known),
            remote_dir="/srv/papers", public_base_url="https://pdf.genemedi.net", min_free_gb=15,
        )
        outputs = iter(["", str(1024)])

        def fake_run(args, **_kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=next(outputs), stderr="")

        with patch("utils.source_pdf_store.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(SourcePdfError, "安全阈值"):
                store.upload(str(self.pdf))


if __name__ == "__main__":
    unittest.main()
