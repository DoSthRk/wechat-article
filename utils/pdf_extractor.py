"""PDF 文本抽取 —— wechat-article Phase 0。

只做最基础的事：给一个 PDF 路径，返回纯文本（按页拼）。Phase 0 假设输入是
文字版 PDF（非扫描件）。扫描件 + OCR 是后续阶段的事。

依赖：pdfplumber（pure-python，安装简单）。如果未来要更快可以切到 pymupdf。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pdfplumber

from utils.logger import setup_logger

logger = setup_logger("pdf_extractor")


class PDFExtractError(Exception):
    """PDF 抽取失败（文件不存在 / 无法解析 / 抽到空文本）。"""


def extract_text(pdf_path: str, max_pages: Optional[int] = None) -> str:
    """抽取 PDF 全文，按页用 ``\n\n--- page N ---\n\n`` 分隔。

    Args:
        pdf_path: PDF 绝对或相对路径。
        max_pages: 最多抽前 N 页；None 抽全部。

    Returns:
        抽取后的纯文本。

    Raises:
        PDFExtractError: 文件不存在、无法打开、或文本完全为空（疑似扫描件）。
    """
    path = Path(pdf_path)
    if not path.exists():
        raise PDFExtractError(f"PDF not found: {pdf_path}")
    if not path.is_file():
        raise PDFExtractError(f"PDF path is not a file: {pdf_path}")

    chunks: list[str] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            total = len(pdf.pages)
            limit = min(max_pages, total) if max_pages else total
            for idx in range(limit):
                page = pdf.pages[idx]
                text = page.extract_text() or ""
                text = text.strip()
                if text:
                    chunks.append(f"--- page {idx + 1} ---\n{text}")
            logger.info(
                "extracted %s: %d/%d pages, %d chunks with text",
                path.name, limit, total, len(chunks),
            )
    except PDFExtractError:
        raise
    except Exception as exc:
        raise PDFExtractError(f"failed to open PDF {pdf_path}: {exc}") from exc

    if not chunks:
        raise PDFExtractError(
            f"PDF {pdf_path} extracted no text. "
            "If this is a scanned PDF, OCR is required (not in Phase 0 scope)."
        )
    return "\n\n".join(chunks)


def extract_metadata(pdf_path: str) -> dict[str, str]:
    """抽取 PDF 元数据（标题/作者/页数）。失败返回空 dict，不抛错。"""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            meta = pdf.metadata or {}
            return {
                "title": str(meta.get("Title", "") or "").strip(),
                "author": str(meta.get("Author", "") or "").strip(),
                "page_count": str(len(pdf.pages)),
            }
    except Exception as exc:
        logger.warning("metadata extract failed for %s: %s", pdf_path, exc)
        return {}
