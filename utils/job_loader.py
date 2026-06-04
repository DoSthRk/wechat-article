"""任务清单加载器 —— wechat-article Phase 0。

``inputs/jobs.yaml`` 是项目的主入口配置：列出所有待生成的文章任务，每个任务
是一个 ``(pdf, template, product, image_pool)`` 4-元组（image_pool 在 Phase 1
才用，Phase 0 留空字段即可）。

YAML schema：
```yaml
jobs:
  - job_id: 2026-05-27-001        # 必填，全局唯一
    pdf: inputs/pdfs/foo.pdf      # 必填，PDF 路径（相对项目根或绝对）
    template: academic_review     # 必填，对应 inputs/style_templates/{id}.yaml
    product: product_a            # 必填，对应 inputs/products/{id}.yaml
    line: aav                     # 可选，内容线 id（aav / solidex）；决定 prompts/lines/{line}.md 写作侧重
    image_pool: gene_therapy      # Phase 1 用；Phase 0 可省
    title_hint: "可选的标题提示"   # 可省，给 LLM 一个起标题的方向
```
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from utils.logger import setup_logger

logger = setup_logger("job_loader")


class JobLoadError(Exception):
    """jobs.yaml 加载/校验失败。"""


@dataclass(frozen=True)
class Job:
    """单个文章生成任务（不可变 DTO）。"""
    job_id: str
    pdf: str
    template: str
    product: str
    line: Optional[str] = None      # 内容线 id（aav / solidex）；决定写作侧重 overlay
    image_pool: Optional[str] = None
    title_hint: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def load_jobs(jobs_yaml_path: str, project_root: Optional[str] = None) -> List[Job]:
    """读 jobs.yaml → List[Job]。

    Args:
        jobs_yaml_path: jobs.yaml 路径。
        project_root: 项目根目录；用来把相对路径转绝对。默认取 jobs_yaml_path
            的父目录的父目录（即 ``inputs/`` 的上级）。

    Raises:
        JobLoadError: 文件/字段错误、job_id 重复、引用文件不存在等。
    """
    path = Path(jobs_yaml_path)
    if not path.exists():
        raise JobLoadError(f"jobs.yaml not found: {jobs_yaml_path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise JobLoadError(f"invalid YAML {jobs_yaml_path}: {exc}") from exc

    items = raw.get("jobs") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise JobLoadError(f"{jobs_yaml_path}: top-level 'jobs' must be a list")

    root = Path(project_root) if project_root else path.resolve().parent.parent
    seen_ids: set[str] = set()
    jobs: List[Job] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise JobLoadError(f"jobs[{idx}] must be a mapping")
        job_id = str(item.get("job_id") or "").strip()
        if not job_id:
            raise JobLoadError(f"jobs[{idx}]: 'job_id' is required")
        if job_id in seen_ids:
            raise JobLoadError(f"jobs[{idx}]: duplicate job_id '{job_id}'")
        seen_ids.add(job_id)

        pdf = str(item.get("pdf") or "").strip()
        template = str(item.get("template") or "").strip()
        product = str(item.get("product") or "").strip()
        if not (pdf and template and product):
            raise JobLoadError(
                f"jobs[{idx}] ({job_id}): pdf/template/product all required"
            )

        # 相对路径转绝对（基于 project_root）
        pdf_abs = pdf if Path(pdf).is_absolute() else str(root / pdf)
        if not Path(pdf_abs).exists():
            raise JobLoadError(f"jobs[{idx}] ({job_id}): pdf not found: {pdf_abs}")

        jobs.append(
            Job(
                job_id=job_id,
                pdf=pdf_abs,
                template=template,
                product=product,
                line=(str(item.get("line")).strip() if item.get("line") else None),
                image_pool=(item.get("image_pool") or None),
                title_hint=(item.get("title_hint") or None),
                extra=dict(item.get("extra") or {}),
            )
        )
    logger.info("loaded %d jobs from %s", len(jobs), jobs_yaml_path)
    return jobs
