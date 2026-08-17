"""Publish original article PDFs and verify their public GeneMedi URL.

The default production backend copies PDFs to ``genemedi.net`` over SSH.  An
OSS backend is also available as a capacity fallback.  Objects are addressed by
their SHA-256 digest, so re-uploading the same paper is idempotent.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib import request as urllib_request
from urllib.parse import quote


class SourcePdfError(Exception):
    """A validation, upload, configuration, or public-read failure."""


@dataclass(frozen=True)
class PublishedSourcePdf:
    url: str
    sha256: str
    size: int


_SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
_SAFE_SSH_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SSH_HOST = "65.49.212.57"
DEFAULT_SSH_USER = "gm_pdf_uploader"
DEFAULT_SSH_PRIVATE_KEY = str(_PROJECT_ROOT / "runtime" / "source_pdf_ssh" / "genemedi-net-pdf_ed25519")
DEFAULT_SSH_KNOWN_HOSTS = str(_PROJECT_ROOT / "runtime" / "source_pdf_ssh" / "known_hosts")
DEFAULT_SSH_REMOTE_DIR = "/www/wwwroot/genemedi_net/uploads/papers"
DEFAULT_PUBLIC_BASE_URL = "https://www.genemedi.net/uploads/papers"


def inspect_pdf(path_value: str) -> tuple[Path, str, int]:
    """Return ``(path, sha256, size)`` after a minimal PDF validity check."""
    path = Path(path_value)
    if not path.is_file():
        raise SourcePdfError(f"原文 PDF 不存在：{path}")
    size = path.stat().st_size
    if size <= 0:
        raise SourcePdfError(f"原文 PDF 为空：{path.name}")
    digest = hashlib.sha256()
    first = b""
    with path.open("rb") as handle:
        first = handle.read(1024)
        digest.update(first)
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if b"%PDF-" not in first:
        raise SourcePdfError(f"原文文件不是有效 PDF：{path.name}")
    return path, digest.hexdigest(), size


def provision_source_pdf_ssh(env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Create the service-owned SSH identity and pinned host key once.

    Only the public key and fingerprint are returned.  The private key remains
    mode 0600 in the shared runtime directory owned by the application user.
    """
    values = env or os.environ
    host = values.get("SOURCE_PDF_SSH_HOST", DEFAULT_SSH_HOST).strip()
    private_key = Path(values.get("SOURCE_PDF_SSH_PRIVATE_KEY", DEFAULT_SSH_PRIVATE_KEY).strip())
    known_hosts = Path(values.get("SOURCE_PDF_SSH_KNOWN_HOSTS", DEFAULT_SSH_KNOWN_HOSTS).strip())
    if not _SAFE_SSH_NAME.fullmatch(host):
        raise SourcePdfError("SOURCE_PDF_SSH_HOST 含非法字符")
    try:
        private_key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_key.parent.chmod(0o700)
        if not private_key.is_file():
            generated = subprocess.run(
                [
                    "ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                    "-C", "gm-blog source pdf uploader", "-f", str(private_key),
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, check=False,
            )
            if generated.returncode != 0:
                raise SourcePdfError(
                    "生成原文 PDF SSH 密钥失败：" + (generated.stderr or generated.stdout).strip()
                )
        scanned = subprocess.run(
            ["ssh-keyscan", "-H", host], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
        )
        if scanned.returncode != 0 or not scanned.stdout.strip():
            raise SourcePdfError("获取 genemedi.net SSH host key 失败：" + scanned.stderr.strip())
        known_hosts.write_text(scanned.stdout, encoding="utf-8")
        private_key.chmod(0o600)
        known_hosts.chmod(0o600)
        public_path = Path(f"{private_key}.pub")
        public_path.chmod(0o644)
        public_key = public_path.read_text(encoding="utf-8").strip()
        fingerprint_result = subprocess.run(
            ["ssh-keygen", "-lf", str(public_path), "-E", "sha256"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        )
        if fingerprint_result.returncode != 0:
            raise SourcePdfError("读取原文 PDF SSH 公钥指纹失败")
    except SourcePdfError:
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourcePdfError(f"初始化原文 PDF SSH 身份失败：{exc}") from exc
    return {
        "public_key": public_key,
        "fingerprint": fingerprint_result.stdout.strip(),
    }


def verify_public_pdf(url: str, *, timeout: float = 20.0) -> None:
    """Verify that a public URL returns readable PDF bytes over HTTP(S)."""
    if not url.lower().startswith("https://"):
        raise SourcePdfError(f"原文 PDF 必须使用 HTTPS：{url}")
    request = urllib_request.Request(
        url,
        headers={"Range": "bytes=0-1023", "User-Agent": "GeneMedi-PDF-Verify/1.0"},
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            content_type = (response.headers.get_content_type() or "").lower()
            first = response.read(1024)
    except Exception as exc:
        raise SourcePdfError(f"原文 PDF 公网校验失败：{url}：{exc}") from exc
    if status not in {200, 206}:
        raise SourcePdfError(f"原文 PDF 公网状态异常：HTTP {status}")
    if content_type != "application/pdf":
        raise SourcePdfError(f"原文 PDF Content-Type 异常：{content_type or 'missing'}")
    if b"%PDF-" not in first:
        raise SourcePdfError("原文 PDF 公网内容校验失败：响应中没有 PDF 文件头")


class SshSourcePdfStore:
    """Atomic SCP uploader for the legacy ``genemedi.net`` webroot."""

    def __init__(
        self,
        *,
        host: str,
        user: str,
        private_key: str,
        known_hosts: str,
        remote_dir: str,
        public_base_url: str,
        port: int = 22,
        min_free_gb: float = 15.0,
        timeout: int = 30,
    ) -> None:
        if not _SAFE_SSH_NAME.fullmatch(host) or not _SAFE_SSH_NAME.fullmatch(user):
            raise SourcePdfError("SOURCE_PDF_SSH_HOST/USER 含非法字符")
        if not _SAFE_REMOTE_PATH.fullmatch(remote_dir):
            raise SourcePdfError("SOURCE_PDF_SSH_REMOTE_DIR 必须是安全的绝对路径")
        self.host = host
        self.user = user
        self.private_key = str(Path(private_key))
        self.known_hosts = str(Path(known_hosts))
        self.remote_dir = remote_dir.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/")
        self.port = int(port)
        self.min_free_bytes = max(0, int(float(min_free_gb) * 1024 ** 3))
        self.timeout = int(timeout)
        if not Path(self.private_key).is_file():
            raise SourcePdfError(f"原文 PDF SSH 私钥不存在：{self.private_key}")
        if not Path(self.known_hosts).is_file():
            raise SourcePdfError(f"原文 PDF SSH known_hosts 不存在：{self.known_hosts}")
        if not self.public_base_url.lower().startswith("https://"):
            raise SourcePdfError("SOURCE_PDF_PUBLIC_BASE_URL 必须使用 HTTPS")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SshSourcePdfStore":
        values = env or os.environ
        required = {
            "host": values.get("SOURCE_PDF_SSH_HOST", DEFAULT_SSH_HOST).strip(),
            "user": values.get("SOURCE_PDF_SSH_USER", DEFAULT_SSH_USER).strip(),
            "private_key": values.get("SOURCE_PDF_SSH_PRIVATE_KEY", DEFAULT_SSH_PRIVATE_KEY).strip(),
            "known_hosts": values.get("SOURCE_PDF_SSH_KNOWN_HOSTS", DEFAULT_SSH_KNOWN_HOSTS).strip(),
            "remote_dir": values.get("SOURCE_PDF_SSH_REMOTE_DIR", DEFAULT_SSH_REMOTE_DIR).strip(),
            "public_base_url": values.get("SOURCE_PDF_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise SourcePdfError("缺少原文 PDF SSH 配置：" + ", ".join(missing))
        return cls(
            **required,
            port=int(values.get("SOURCE_PDF_SSH_PORT", "22") or 22),
            min_free_gb=float(values.get("SOURCE_PDF_MIN_FREE_GB", "15") or 15),
            timeout=int(values.get("SOURCE_PDF_UPLOAD_TIMEOUT", "30") or 30),
        )

    def _ssh_base(self) -> list[str]:
        return [
            "ssh", "-i", self.private_key, "-p", str(self.port),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", f"ConnectTimeout={self.timeout}", f"{self.user}@{self.host}",
        ]

    def _run_ssh(self, command: str) -> str:
        try:
            result = subprocess.run(
                [*self._ssh_base(), command], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourcePdfError(f"原文 PDF SSH 执行失败：{exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise SourcePdfError(f"原文 PDF SSH 返回 {result.returncode}：{detail}")
        return result.stdout.strip()

    def upload(self, local_path: str) -> PublishedSourcePdf:
        path, digest, size = inspect_pdf(local_path)
        shard = digest[:2]
        remote_subdir = f"{self.remote_dir}/{shard}"
        remote_path = f"{remote_subdir}/{digest}.pdf"
        remote_tmp = f"{remote_path}.part"
        self._run_ssh(f"mkdir -p {remote_subdir}")
        available_text = self._run_ssh(f"df -Pk {remote_subdir} | tail -n 1 | awk '{{print $4}}'")
        try:
            available_bytes = int(available_text.splitlines()[-1]) * 1024
        except (ValueError, IndexError) as exc:
            raise SourcePdfError(f"无法读取 genemedi.net 剩余容量：{available_text!r}") from exc
        if available_bytes < self.min_free_bytes:
            raise SourcePdfError(
                f"genemedi.net 剩余容量低于安全阈值：{available_bytes / 1024 ** 3:.1f}GB"
            )
        exists = self._run_ssh(
            f"if test -f {remote_path} && test $(stat -c %s {remote_path}) -eq {size}; then echo yes; else echo no; fi"
        )
        if exists != "yes":
            scp = [
                "scp", "-i", self.private_key, "-P", str(self.port),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", f"ConnectTimeout={self.timeout}", str(path),
                f"{self.user}@{self.host}:{remote_tmp}",
            ]
            try:
                result = subprocess.run(
                    scp, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=max(self.timeout, 120), check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SourcePdfError(f"原文 PDF SCP 上传失败：{exc}") from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown error").strip()
                raise SourcePdfError(f"原文 PDF SCP 返回 {result.returncode}：{detail}")
            self._run_ssh(
                f"test $(stat -c %s {remote_tmp}) -eq {size} && chmod 0644 {remote_tmp} && mv -f {remote_tmp} {remote_path}"
            )
        url = f"{self.public_base_url}/{shard}/{digest}.pdf"
        verify_public_pdf(url, timeout=float(self.timeout))
        return PublishedSourcePdf(url=url, sha256=digest, size=size)


class OssSourcePdfStore:
    """Content-addressed OSS uploader used when the origin disk is unsuitable."""

    def __init__(self, bucket: Any, prefix: str, public_base_url: str) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.public_base_url = public_base_url.rstrip("/")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "OssSourcePdfStore":
        values = env or os.environ
        config = {
            "access_id": (values.get("SOURCE_PDF_OSS_ACCESS_KEY_ID") or values.get("ALIYUN_OSS_ACCESS_KEY_ID") or "").strip(),
            "access_secret": (values.get("SOURCE_PDF_OSS_ACCESS_KEY_SECRET") or values.get("ALIYUN_OSS_ACCESS_KEY_SECRET") or "").strip(),
            "endpoint": (values.get("SOURCE_PDF_OSS_ENDPOINT") or values.get("ALIYUN_OSS_ENDPOINT") or "").strip(),
            "bucket": (values.get("SOURCE_PDF_OSS_BUCKET") or values.get("ALIYUN_OSS_BUCKET") or "").strip(),
            "public_base_url": values.get("SOURCE_PDF_PUBLIC_BASE_URL", "").strip(),
        }
        missing = [name for name, value in config.items() if not value]
        if missing:
            raise SourcePdfError("缺少原文 PDF OSS 配置：" + ", ".join(missing))
        try:
            import oss2
        except ImportError as exc:
            raise SourcePdfError("oss2 未安装，无法上传原文 PDF") from exc
        bucket = oss2.Bucket(
            oss2.Auth(config["access_id"], config["access_secret"]),
            config["endpoint"], config["bucket"],
        )
        return cls(
            bucket,
            values.get("SOURCE_PDF_OSS_PREFIX", "article-assets/source-pdfs"),
            config["public_base_url"],
        )

    def upload(self, local_path: str) -> PublishedSourcePdf:
        path, digest, size = inspect_pdf(local_path)
        key = "/".join(part for part in (self.prefix, digest[:2], f"{digest}.pdf") if part)
        try:
            if not self.bucket.object_exists(key):
                self.bucket.put_object_from_file(
                    key,
                    str(path),
                    headers={
                        "Content-Type": "application/pdf",
                        "Cache-Control": "public, max-age=31536000, immutable",
                    },
                )
        except Exception as exc:
            raise SourcePdfError(f"原文 PDF OSS 上传失败：{path.name}：{exc}") from exc
        url = f"{self.public_base_url}/{quote(key, safe='/')}"
        verify_public_pdf(url)
        return PublishedSourcePdf(url=url, sha256=digest, size=size)


def create_source_pdf_store(env: Optional[Mapping[str, str]] = None):
    values = env or os.environ
    mode = values.get("SOURCE_PDF_STORAGE", "ssh").strip().lower()
    if mode == "ssh":
        return SshSourcePdfStore.from_env(values)
    if mode == "oss":
        return OssSourcePdfStore.from_env(values)
    raise SourcePdfError(f"不支持的 SOURCE_PDF_STORAGE：{mode}")
