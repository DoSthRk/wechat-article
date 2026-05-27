"""ArticleAnalyzer —— wechat-article Phase 0 单线版。

输入：``Job``（PDF + 模板 + 产品）
处理：抽 PDF 文本 → 拼 system prompt → 调一次 LLM → 解析出 markdown
输出：``AnalysisResult``（markdown + 元数据；不写 DB / 不发布，交由 batch_processor）

Phase 3 才加 3 路并行 + reviewer + merger；这里先保持 200 行内的精简实现，
方便验证端到端是否通畅。
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
_PROMPT_PATH = _BASE_DIR / "prompts" / "article_writer.system.md"


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
    """单线版：1 PDF + 1 模板 + 1 产品 → 1 markdown。"""

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
        self._system_prompt = self._load_system_prompt()

    @staticmethod
    def _load_system_prompt() -> str:
        if not _PROMPT_PATH.exists():
            raise RuntimeError(f"system prompt missing: {_PROMPT_PATH}")
        return _PROMPT_PATH.read_text(encoding="utf-8")

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

        user_message = self._build_user_message(job, pdf_text, template, product)
        logger.info(
            "calling LLM model=%s job=%s pdf=%s tpl=%s product=%s",
            self.model, job.job_id, Path(job.pdf).name, job.template, job.product,
        )

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                messages=[
                    {"role": "system", "content": self._system_prompt},
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
        """组装 user message：PDF 原文 + 风格约束 + 产品信息 + 任务指令。"""
        parts = [
            template.to_prompt_block(),
            "",
            "---",
            "",
            product.to_prompt_block(),
            "",
            "---",
            "",
            "# 核心素材（来自 PDF）",
            "",
            "下面是 PDF 抽取出的原文。**这是你写作的事实依据，所有论点必须有出处**；",
            "但**不要整段照抄**，要重新组织、消化、用公众号读者能理解的语言改写。",
            "",
            pdf_text,
            "",
            "---",
            "",
            "# 任务",
            "",
            f"写一篇符合上述模板约束的公众号文章，主题围绕这份 PDF 的核心内容，",
            f"在合适位置自然融入 \"{product.name}\" 作为软广（**绝不硬塞**）。",
        ]
        if job.title_hint:
            parts.append(f"\n标题方向参考：{job.title_hint}（不必照搬，可改）")
        parts.extend([
            "",
            "## 输出契约",
            "- 只输出 markdown 正文，**不要**包在 ```markdown ... ``` 围栏里",
            "- 第一行必须是 `# 标题`",
            "- 在合适位置插入图片占位符 `[图片:此处建议展示xxx描述]`，",
            "  描述要具体（用于后续图库匹配），中文，不要超过 30 字",
            "- 全文遵守模板的禁用词约束、字数区间、调性关键词",
            "- 软广要求：产品名最多出现 2-3 次，要在论述自然推进到那个点位时再提",
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
