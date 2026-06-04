"""内容线（line）加载器 —— AAV / Solidex 等。

``line`` 是项目的一等概念：一条内容线 = ``{写作侧重(prompt overlay) + 默认模板 + 固定产品}``。
配置在 ``inputs/lines/{line_id}.yaml``。

方案 B 下，line 决定：
- 写作侧重：``prompts/lines/{prompt_overlay}.md``，叠加在 ``base.system.md`` 之后；
- 该线固定产品：结尾软广点名（人工选品，AI 只负责措辞）；
- 该线默认风格模板。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml

from utils.logger import setup_logger

logger = setup_logger("line_loader")


class LineLoadError(Exception):
    """line 配置加载 / 校验失败。"""


@dataclass(frozen=True)
class Line:
    """单条内容线（不可变 DTO）。"""
    line_id: str
    name: str
    template: str               # 默认风格模板 id
    product: str                # 固定产品 id（方案 B 结尾点名）
    prompt_overlay: str         # prompts/lines/{prompt_overlay}.md
    audience: str = ""
    description: str = ""
    status: str = "active"
    extra: Dict[str, Any] = field(default_factory=dict)


def load_line(line_path: str) -> Line:
    """读 YAML → Line；缺必填字段抛 LineLoadError。"""
    path = Path(line_path)
    if not path.exists():
        raise LineLoadError(f"line config not found: {line_path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise LineLoadError(f"invalid YAML {line_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise LineLoadError(f"line root must be a mapping: {line_path}")

    line_id = str(raw.get("line_id") or path.stem).strip()
    name = str(raw.get("name") or "").strip()
    template = str(raw.get("template") or "").strip()
    product = str(raw.get("product") or "").strip()
    if not name:
        raise LineLoadError(f"line {line_path}: 'name' is required")
    if not template:
        raise LineLoadError(f"line {line_path}: 'template' is required")
    if not product:
        raise LineLoadError(f"line {line_path}: 'product' is required")
    return Line(
        line_id=line_id,
        name=name,
        template=template,
        product=product,
        prompt_overlay=(str(raw.get("prompt_overlay") or "").strip() or line_id),
        audience=str(raw.get("audience") or ""),
        description=str(raw.get("description") or ""),
        status=(str(raw.get("status") or "active").strip() or "active"),
        extra=dict(raw.get("extra") or {}),
    )


def load_line_by_id(lines_dir: str, line_id: str) -> Line:
    """从 line 目录按 id 取 → ``{lines_dir}/{line_id}.yaml``。"""
    p = Path(lines_dir) / f"{line_id}.yaml"
    return load_line(str(p))
