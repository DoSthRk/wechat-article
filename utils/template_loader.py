"""风格模板加载器 —— wechat-article Phase 0。

风格模板用 YAML 描述"想要的文章长什么样"，但**不塞范文**到 prompt 里 ——
prompt 只看模板里的硬约束（长度区间、章节数、调性、禁用词）和柔性引导
（首段范式、结尾呼应模式等）。

示例见 ``inputs/style_templates/academic_review.example.yaml``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from utils.logger import setup_logger

logger = setup_logger("template_loader")


class TemplateLoadError(Exception):
    """模板加载/校验失败。"""


@dataclass(frozen=True)
class StyleTemplate:
    """单个风格模板（不可变 DTO）。"""
    template_id: str            # 文件名（去 .yaml）
    name: str                   # 显示名
    description: str            # 一句话描述
    target_length: Dict[str, int]   # {"min": N, "max": M}（中文字数）
    section_count: Dict[str, int]   # {"min": N, "max": M}
    tone_keywords: List[str]    # 期望调性的关键词，如["学术中立","克制","数据驱动"]
    forbidden_phrases: List[str]    # 禁用的硬广词/夸张词
    opening_guidance: str       # 首段建议（如"用一个反常识的事实或数据起手"）
    closing_guidance: str       # 结尾建议
    extra: Dict[str, Any] = field(default_factory=dict)  # 任意扩展字段（透传给 prompt）

    def to_prompt_block(self) -> str:
        """渲染成可塞进 system prompt 的纯文本块。"""
        lines = [
            f"# 风格模板：{self.name}",
            f"描述：{self.description}",
            "",
            f"## 长度约束",
            f"- 中文字数区间：{self.target_length.get('min', 800)} - {self.target_length.get('max', 2500)}",
            f"- 章节数区间：{self.section_count.get('min', 3)} - {self.section_count.get('max', 6)}",
            "",
            f"## 调性",
            f"- 期望关键词：{', '.join(self.tone_keywords) if self.tone_keywords else '（未指定）'}",
            f"- 禁用词/短语：{', '.join(self.forbidden_phrases) if self.forbidden_phrases else '（未指定）'}",
            "",
            f"## 首段指引",
            f"{self.opening_guidance or '（无）'}",
            "",
            f"## 结尾指引",
            f"{self.closing_guidance or '（无）'}",
        ]
        if self.extra:
            lines.extend(["", "## 额外约束"])
            for k, v in self.extra.items():
                lines.append(f"- {k}: {v}")
        return "\n".join(lines)


def load_template(template_path: str) -> StyleTemplate:
    """读 YAML → StyleTemplate；缺字段抛 TemplateLoadError。"""
    path = Path(template_path)
    if not path.exists():
        raise TemplateLoadError(f"template not found: {template_path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise TemplateLoadError(f"invalid YAML {template_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise TemplateLoadError(f"template root must be a mapping: {template_path}")

    template_id = path.stem
    try:
        return StyleTemplate(
            template_id=template_id,
            name=str(raw.get("name") or template_id),
            description=str(raw.get("description") or ""),
            target_length=dict(raw.get("target_length") or {"min": 800, "max": 2500}),
            section_count=dict(raw.get("section_count") or {"min": 3, "max": 6}),
            tone_keywords=list(raw.get("tone_keywords") or []),
            forbidden_phrases=list(raw.get("forbidden_phrases") or []),
            opening_guidance=str(raw.get("opening_guidance") or ""),
            closing_guidance=str(raw.get("closing_guidance") or ""),
            extra=dict(raw.get("extra") or {}),
        )
    except (TypeError, ValueError) as exc:
        raise TemplateLoadError(f"template schema error in {template_path}: {exc}") from exc


def load_template_by_id(templates_dir: str, template_id: str) -> StyleTemplate:
    """从模板目录按 id 取 → ``{templates_dir}/{template_id}.yaml``。"""
    p = Path(templates_dir) / f"{template_id}.yaml"
    return load_template(str(p))
