"""产品信息加载器 —— wechat-article Phase 0。

每篇文章软广一个产品。产品 YAML 描述：名称 / 卖点 / 规格 / 适用场景 / 忌讳描述。
prompt 时被渲染成一个"产品信息块"喂给 LLM，让它在文中自然融入而不是硬塞。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml

from utils.logger import setup_logger

logger = setup_logger("product_loader")


class ProductLoadError(Exception):
    """产品 YAML 加载/校验失败。"""


@dataclass(frozen=True)
class Product:
    """单个产品（不可变 DTO）。"""
    product_id: str
    name: str
    one_liner: str              # 一句话定位
    selling_points: List[str]   # 关键卖点（3-5 条）
    specs: Dict[str, str]       # 规格参数（任意键值对）
    use_cases: List[str]        # 典型应用场景
    target_users: List[str]     # 目标用户画像
    forbidden_claims: List[str] # 不能说的话（夸张/虚假/合规风险）
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        """渲染成可塞进 system prompt 的纯文本块。"""
        lines = [
            f"# 产品信息：{self.name}",
            f"一句话定位：{self.one_liner}",
            "",
            f"## 核心卖点",
        ]
        if self.selling_points:
            for sp in self.selling_points:
                lines.append(f"- {sp}")
        else:
            lines.append("（未提供）")

        if self.specs:
            lines.extend(["", "## 关键规格"])
            for k, v in self.specs.items():
                lines.append(f"- {k}: {v}")

        if self.use_cases:
            lines.extend(["", "## 典型应用场景"])
            for uc in self.use_cases:
                lines.append(f"- {uc}")

        if self.target_users:
            lines.extend(["", "## 目标用户"])
            for tu in self.target_users:
                lines.append(f"- {tu}")

        if self.forbidden_claims:
            lines.extend(["", "## 严禁宣称（合规红线）"])
            for fc in self.forbidden_claims:
                lines.append(f"- {fc}")

        if self.extra:
            lines.extend(["", "## 补充"])
            for k, v in self.extra.items():
                lines.append(f"- {k}: {v}")

        return "\n".join(lines)


def load_product(product_path: str) -> Product:
    """读 YAML → Product；缺字段抛 ProductLoadError。"""
    path = Path(product_path)
    if not path.exists():
        raise ProductLoadError(f"product not found: {product_path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ProductLoadError(f"invalid YAML {product_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ProductLoadError(f"product root must be a mapping: {product_path}")

    product_id = path.stem
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ProductLoadError(f"product {product_path}: 'name' is required")
    try:
        return Product(
            product_id=product_id,
            name=name,
            one_liner=str(raw.get("one_liner") or ""),
            selling_points=list(raw.get("selling_points") or []),
            specs=dict(raw.get("specs") or {}),
            use_cases=list(raw.get("use_cases") or []),
            target_users=list(raw.get("target_users") or []),
            forbidden_claims=list(raw.get("forbidden_claims") or []),
            extra=dict(raw.get("extra") or {}),
        )
    except (TypeError, ValueError) as exc:
        raise ProductLoadError(f"product schema error in {product_path}: {exc}") from exc


def load_product_by_id(products_dir: str, product_id: str) -> Product:
    """从产品目录按 id 取 → ``{products_dir}/{product_id}.yaml``。"""
    p = Path(products_dir) / f"{product_id}.yaml"
    return load_product(str(p))
