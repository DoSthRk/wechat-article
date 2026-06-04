"""ArticleAnalyzer —— wechat-article 内容生成（方案 B 单路版）。

输入：``Job``（PDF + 模板 + 固定产品 + line）
处理：抽 PDF 文本 → 拼 system prompt（共通基底 base + 该 line 写作侧重 overlay）
      → 调一次 LLM → 解析出 markdown
输出：``AnalysisResult``（markdown + 元数据；不写 DB / 不发布，交由 batch_processor）

方案 B（产品植入）：
- 正文纯科普，**正文中绝不出现任何产品名 / 品牌 / 公司**；
- 只在**结尾一段**自然点名本线的**固定产品**（人工选品，AI 只负责措辞）；
- 运行时把"产品名 + 融入角度（closing_hint）+ 合规红线"注入 user message。

多模型并行 / reviewer / merger 是后续阶段（P6'），这里保持单路精简实现。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from openai import OpenAI

from utils.job_loader import Job
from utils.logger import setup_logger
from utils.pdf_extractor import extract_text
from utils.product_loader import Product, load_product_by_id
from utils.template_loader import StyleTemplate, load_template_by_id

load_dotenv()
logger = setup_logger("article_analyzer")

_BASE_DIR = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _BASE_DIR / "prompts"
_BASE_PROMPT_PATH = _PROMPTS_DIR / "base.system.md"
_LINE_PROMPT_DIR = _PROMPTS_DIR / "lines"


@dataclass
class AnalysisResult:
    job_id: str
    success: bool
    markdown: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    error_message: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class ArticleAnalyzer:
    """单路版：1 PDF + 1 模板 + 1 固定产品（line 决定写作侧重）→ 1 markdown（方案 B）。"""

    def __init__(
        self,
        templates_dir: Optional[str] = None,
        products_dir: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ):
        self.templates_dir = templates_dir or str(_BASE_DIR / "inputs" / "style_templates")
        self.products_dir = products_dir or str(_BASE_DIR / "inputs" / "products")

        self.api_key = (api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not configured")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")).strip()
        self.model = (model or os.getenv("ARTICLE_WRITER_MODEL", "deepseek-v4-flash")).strip()
        self.max_tokens = int(max_tokens or os.getenv("ARTICLE_WRITER_MAX_TOKENS", "8000"))
        self.timeout = float(timeout or os.getenv("ARTICLE_WRITER_TIMEOUT", "240"))
        self.temperature = float(os.getenv("ARTICLE_WRITER_TEMPERATURE", "0.7"))

        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
        self._base_prompt = self._load_base_prompt()
        self._overlay_cache: Dict[str, str] = {}

    @staticmethod
    def _load_base_prompt() -> str:
        if not _BASE_PROMPT_PATH.exists():
            raise RuntimeError(f"base system prompt missing: {_BASE_PROMPT_PATH}")
        return _BASE_PROMPT_PATH.read_text(encoding="utf-8")

    def _load_line_overlay(self, line_id: Optional[str]) -> str:
        """按 line 读 ``prompts/lines/{line}.md``；无 line 或文件缺失返回空串。"""
        key = (line_id or "").strip()
        if not key:
            return ""
        if key in self._overlay_cache:
            return self._overlay_cache[key]
        path = _LINE_PROMPT_DIR / f"{key}.md"
        overlay = path.read_text(encoding="utf-8") if path.exists() else ""
        if not overlay:
            logger.warning("line overlay not found for line=%s (%s)", key, path)
        self._overlay_cache[key] = overlay
        return overlay

    def _system_prompt_for(self, job: Job) -> str:
        """共通基底 base + 该 line 写作侧重 overlay。"""
        overlay = self._load_line_overlay(job.line)
        if overlay:
            return f"{self._base_prompt}\n\n---\n\n{overlay}"
        return self._base_prompt

    def analyze(self, job: Job) -> AnalysisResult:
        """跑一个 job。任何异常被吞下，returned in ``error_message``。"""
        started = time.time()
        try:
            template = load_template_by_id(self.templates_dir, job.template)
            product = load_product_by_id(self.products_dir, job.product)
            pdf_text = extract_text(job.pdf)
        except Exception as exc:
            return AnalysisResult(
                job_id=job.job_id, success=False,
                error_message=f"input load failed: {exc}",
                latency_ms=int((time.time() - started) * 1000),
            )

        system_prompt = self._system_prompt_for(job)
        user_message = self._build_user_message(job, pdf_text, template, product)
        logger.info(
            "calling LLM model=%s job=%s line=%s pdf=%s tpl=%s product=%s",
            self.model, job.job_id, job.line, Path(job.pdf).name, job.template, job.product,
        )

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
            content = (response.choices[0].message.content or "").strip()
            usage = getattr(response, "usage", None)
            pt = int(getattr(usage, "prompt_tokens", 0) or 0)
            ct = int(getattr(usage, "completion_tokens", 0) or 0)
            tt = int(getattr(usage, "total_tokens", 0) or (pt + ct))
        except Exception as exc:
            return AnalysisResult(
                job_id=job.job_id, success=False, model=self.model,
                error_message=f"LLM call failed: {exc}",
                latency_ms=int((time.time() - started) * 1000),
            )

        # 容错：模型偶尔会把 ```markdown\n...\n``` 包起来
        content = self._strip_outer_code_fence(content)

        if not content or not content.strip().startswith("#"):
            return AnalysisResult(
                job_id=job.job_id, success=False, model=self.model,
                markdown=content[:500],
                prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
                error_message=(
                    f"LLM output not valid markdown (does not start with '#'); "
                    f"len={len(content)} head={content[:120]!r}"
                ),
                latency_ms=int((time.time() - started) * 1000),
            )

        return AnalysisResult(
            job_id=job.job_id, success=True, model=self.model,
            markdown=content,
            prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            latency_ms=int((time.time() - started) * 1000),
        )

    @staticmethod
    def _build_user_message(
        job: Job, pdf_text: str, template: StyleTemplate, product: Product,
    ) -> str:
        """组装 user message（方案 B）：风格约束 + PDF 原文 + 结尾产品信息 + 任务。

        注意：**不再**把整块产品信息（卖点 / 规格）喂进正文——产品只在结尾出现，
        生成侧只需要"产品名 + 融入角度 + 合规红线"。
        """
        closing_hint = (getattr(product, "closing_hint", "") or "").strip()
        parts = [
            template.to_prompt_block(),
            "",
            "---",
            "",
            "# PDF 原文（你的唯一事实地基）",
            "",
            "下面是 PDF 抽取出的原文。所有论点、数据、配图都必须出自这里；",
            "**不要整段照抄**，要重新组织、消化、用读者能懂的语言改写。",
            "",
            pdf_text,
            "",
            "---",
            "",
            "# 结尾产品信息（⚠️ 仅用于文章最后一段；正文严禁出现）",
            "",
            f"- 固定产品名（人工锁定，**不得更换、也不得新增**别的产品）：{product.name}",
            f"- 融入角度：{closing_hint or '（无特别要求，自然贴合本篇主题即可）'}",
        ]
        if product.forbidden_claims:
            parts.append("- 合规红线（**绝不违反**）：")
            parts.extend(f"  - {fc}" for fc in product.forbidden_claims)
        parts.extend([
            "",
            "---",
            "",
            "# 任务",
            "",
            "写一篇符合上述模板约束的科普文章，主题围绕这份 PDF 的核心内容。",
            "**正文（结尾产品段之前的全部内容）绝不出现任何产品名 / 品牌 / 公司名 / 链接**；",
            "只在**最后一段**自然、克制地点名上面给定的固定产品**一次**——"
            "像中立智库顺手提一个恰好合适的工具，去乙方化、润物细无声。",
        ])
        if job.title_hint:
            parts.append(f"\n标题方向参考：{job.title_hint}（不必照搬，可改；标题里不要出现产品名）")
        parts.extend([
            "",
            "## 输出契约",
            "- 只输出 markdown 正文，**不要**包在 ```markdown ... ``` 围栏里",
            "- 第一行必须是 `# 标题`",
            "- 配图占位符 `[图片:Figure X 描述]` **只能来自 PDF 里的图**，全文 3-4 张，描述具体、中文、≤30 字",
            "- 全文遵守模板的禁用词约束、字数区间、调性关键词",
            "- 产品**只在结尾出现一次**，正文零产品",
        ])
        return "\n".join(parts)

    @staticmethod
    def _strip_outer_code_fence(text: str) -> str:
        t = (text or "").strip()
        if t.startswith("```"):
            # 去首行围栏
            first_nl = t.find("\n")
            if first_nl > 0:
                t = t[first_nl + 1:]
        if t.endswith("```"):
            t = t[: t.rfind("```")].rstrip()
        return t
