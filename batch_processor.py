"""wechat-article 主入口 —— generate / distribute 两阶段。

阶段（``--stage``）：
    generate  ：jobs.yaml → 逐 job 生成基准正文（方案 B）→ 落盘 + 写 articles 表
    distribute：逐 job 取基准正文 → 投放到平台 distribution（当前只接公众号 wechat；
                blog / linkedin 是 Phase 4）。account 从 line 配置的 wechat_account 取。
    all       ：先 generate 再 distribute（默认）

内容与投放解耦：一篇基准文章（article）可扇出到多个 distribution（platform × account × lang）。
当前 distribute 只实现公众号单平台；产品模块组装（Phase 3）、多平台（Phase 4）后续接入。

用法：
    python batch_processor.py                       # generate + distribute
    python batch_processor.py --stage generate      # 只生成
    python batch_processor.py --stage distribute    # 只投放（需先 generate）
    python batch_processor.py --dry-run             # 生成但不投放
    python batch_processor.py --only <job_id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# 让 utils / core / db 都能 from-import
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.main import ArticleAnalyzer
from db.database import ARTICLE_CONTENT_DIR, JobStatus, get_db_manager
from utils.health_check import markdown_health_score
from utils.job_loader import Job, load_jobs
from utils.line_loader import LineLoadError, load_line_by_id
from utils.logger import setup_logger
from utils.product_loader import load_product_by_id
from utils.tonal_qa import load_hard_ad_words, scan_static
from utils.wechat_client import WeChatAPIError, WeChatClient
from utils.pdf_figure_extractor import Figure, extract_figures, figure_number, match_figure
from utils.wechat_html import (
    extract_title_and_digest,
    find_image_placeholders,
    markdown_to_wechat_html,
    replace_image_placeholder,
)

load_dotenv()
logger = setup_logger("batch_processor")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_JOBS_YAML = str(PROJECT_ROOT / "inputs" / "jobs.yaml")
LINES_DIR = str(PROJECT_ROOT / "inputs" / "lines")
DATA_DIR = PROJECT_ROOT / "data"
PRODUCTS_DIR = str(PROJECT_ROOT / "inputs" / "products")
RUNTIME_DIR = PROJECT_ROOT / "runtime"

WECHAT_PLATFORM = "wechat"
DEFAULT_LANG = "zh"
HEALTH_THRESHOLD = int(os.getenv("MARKDOWN_HEALTH_THRESHOLD", "30") or 30)
TONAL_THRESHOLD = int(os.getenv("TONAL_BLOCKED_THRESHOLD", "60") or 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="wechat-article batch processor")
    parser.add_argument("--jobs", default=DEFAULT_JOBS_YAML, help="jobs.yaml 路径")
    parser.add_argument(
        "--task",
        default=f"wechat-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        help="task 名（DB 里用）",
    )
    parser.add_argument(
        "--stage", choices=["generate", "distribute", "all"], default="all",
        help="generate=只生成 / distribute=只投放 / all=两者（默认）",
    )
    parser.add_argument("--only", action="append", help="只跑指定 job_id（可多次传）")
    parser.add_argument("--dry-run", action="store_true", help="生成但不投放（distribute 跳过）")
    parser.add_argument(
        "--placeholder-author",
        default=os.getenv("DEFAULT_AUTHOR", "TarMart"),
        help="草稿的 author 字段（公众号要求非空）",
    )
    parser.add_argument(
        "--placeholder-thumb-media",
        default=os.getenv("DEFAULT_THUMB_MEDIA_ID", ""),
        help=(
            "公众号草稿要求 thumb_media_id 非空（封面图素材 id）。"
            "图片管线落地前需手动准备一张永久素材并填 .env DEFAULT_THUMB_MEDIA_ID。"
        ),
    )
    args = parser.parse_args()

    do_generate = args.stage in ("generate", "all")
    do_distribute = args.stage in ("distribute", "all") and not args.dry_run
    if not do_generate and not do_distribute:
        logger.warning("nothing to do (stage=%s dry_run=%s)", args.stage, args.dry_run)
        return 0

    # 加载 + 入库 jobs
    try:
        all_jobs = load_jobs(args.jobs, project_root=str(PROJECT_ROOT))
    except Exception as exc:
        logger.error("load jobs failed: %s", exc)
        return 2

    selected = _filter_jobs(all_jobs, args.only)
    if not selected:
        logger.error("no jobs to run (use --only or check jobs.yaml)")
        return 2

    db = get_db_manager()
    task = db.get_or_create_task(args.task, description="two-stage batch")
    logger.info(
        "task=%s (id=%d) stage=%s will run %d/%d jobs",
        task.task_name, task.id, args.stage, len(selected), len(all_jobs),
    )

    for j in selected:
        # 只 upsert 配置，不强制 status（避免 distribute 阶段把已生成的 job 打回 pending）
        db.upsert_job(
            task.id, j.job_id,
            pdf_path=j.pdf, template_id=j.template, product_id=j.product,
            image_pool=j.image_pool, title_hint=j.title_hint,
        )

    analyzer: Optional[ArticleAnalyzer] = None
    if do_generate:
        try:
            analyzer = ArticleAnalyzer()
        except Exception as exc:
            logger.error("ArticleAnalyzer init failed: %s", exc)
            return 3

    # 投放：按账户惰性建 client（token 隔离）；凭据/封面缺失在 _distribute_one 内按 job 报错
    get_client: Optional[Callable[[str], WeChatClient]] = (
        _make_client_getter() if do_distribute else None
    )

    success = 0
    failed = 0
    for j in selected:
        ok = _run_one_job(db, task.id, j, analyzer, get_client, args, do_generate, do_distribute)
        if ok:
            success += 1
        else:
            failed += 1

    logger.info("done. total=%d success=%d failed=%d", len(selected), success, failed)
    return 0 if failed == 0 else 1


def _filter_jobs(jobs: List[Job], only: Optional[List[str]]) -> List[Job]:
    if not only:
        return jobs
    wanted = set(only)
    return [j for j in jobs if j.job_id in wanted]


def _run_one_job(
    db, task_id: int, job: Job,
    analyzer: Optional[ArticleAnalyzer],
    get_client: Optional[Callable[[str], WeChatClient]],
    args: argparse.Namespace,
    do_generate: bool,
    do_distribute: bool,
) -> bool:
    job_pk = db.upsert_job(task_id, job.job_id).id

    if do_generate:
        if analyzer is None or not _generate_one(db, job_pk, job, analyzer):
            return False

    if do_distribute:
        if get_client is None:
            return True
        return _distribute_one(db, job_pk, job, get_client, args)

    return True


def _generate_one(db, job_pk: int, job: Job, analyzer: ArticleAnalyzer) -> bool:
    """生成阶段：方案 B 出基准正文 → 落盘 → 写 articles 表。"""
    db.update_job_status(job_pk, JobStatus.GENERATING)
    result = analyzer.analyze(job)
    if not result.success:
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=result.error_message)
        logger.error("[%s] generate failed: %s", job.job_id, result.error_message)
        return False

    out_dir = Path(ARTICLE_CONTENT_DIR) / job.job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "article.md").write_text(result.markdown, encoding="utf-8")
    title, digest = extract_title_and_digest(result.markdown)
    html = markdown_to_wechat_html(result.markdown)
    (out_dir / "article.html").write_text(html, encoding="utf-8")

    # 质量自审：健康度 + 调性 + 正文夹带产品（方案 B）
    health = markdown_health_score(result.markdown)
    hard_ad = load_hard_ad_words(str(DATA_DIR / "hard_ad_words.txt"))
    tonal = scan_static(
        result.markdown, hard_ad,
        product_name=_safe_product_name(job), threshold=TONAL_THRESHOLD,
    )
    reasons: List[str] = []
    if health < HEALTH_THRESHOLD:
        reasons.append(f"markdown_unhealthy:{health}")
    if tonal.body_product_leak:
        reasons.append("body_product_leak")
    if tonal.score < TONAL_THRESHOLD:
        reasons.append(f"tonal_low:{tonal.score}")
    publish_blocked = bool(reasons)
    block_reason = ";".join(reasons) or None

    (out_dir / "meta.json").write_text(
        json.dumps({
            "job_id": job.job_id, "line": job.line, "title": title, "digest": digest,
            "model": result.model, "tokens": result.total_tokens,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "latency_ms": result.latency_ms,
            "char_count": len(result.markdown),
            "markdown_health_score": health,
            "tonal_score": tonal.score,
            "publish_blocked": publish_blocked,
            "block_reason": block_reason,
            "generated_at": datetime.utcnow().isoformat(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    db.upsert_article(
        job_pk,
        title=title, digest=digest, content_dir=str(out_dir),
        word_count=len(result.markdown),
        model=result.model,
        prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens, latency_ms=result.latency_ms,
        markdown_health_score=health,
        tonal_score=tonal.score,
        tonal_feedback=json.dumps({
            "hard_ad_hits": tonal.hard_ad_hits,
            "body_product_leak": tonal.body_product_leak,
            "suggestions": tonal.suggestions,
        }, ensure_ascii=False),
        publish_blocked=publish_blocked,
        block_reason=block_reason,
    )
    db.update_job_status(job_pk, JobStatus.GENERATED)
    if publish_blocked:
        logger.warning("[%s] generated but BLOCKED: %s", job.job_id, block_reason)
    logger.info(
        "[%s] generated: title=%s len=%d tokens=%d health=%d tonal=%d%s",
        job.job_id, (title or "?")[:40], len(result.markdown), result.total_tokens,
        health, tonal.score, " [BLOCKED]" if publish_blocked else "",
    )
    return True


def _distribute_one(
    db, job_pk: int, job: Job,
    get_client: Callable[[str], WeChatClient],
    args: argparse.Namespace,
) -> bool:
    """投放阶段：取基准正文 → 该线对应账户的公众号草稿（已有 media_id 则走 PATCH）。"""
    article = db.get_article(job_pk)
    if not article or not article.content_dir:
        db.update_job_status(job_pk, JobStatus.FAILED, error_message="distribute: no article, run --stage generate first")
        logger.error("[%s] distribute: 还没生成基准正文", job.job_id)
        return False
    if getattr(article, "publish_blocked", False):
        # 质量闸拦下：稿子已落盘供人工 review，但不投放（不算失败）
        logger.warning("[%s] 质量闸拦下，跳过投放：%s", job.job_id, article.block_reason)
        return True
    md_path = Path(article.content_dir) / "article.md"
    if not md_path.exists():
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=f"distribute: missing {md_path}")
        logger.error("[%s] distribute: 缺 article.md (%s)", job.job_id, md_path)
        return False
    # 从 article.md（唯一事实源）实时渲染 HTML —— 标题样式等改动无需重生成即可生效
    html = markdown_to_wechat_html(md_path.read_text(encoding="utf-8"))
    account = _resolve_wechat_account(job)
    try:
        client = get_client(account)
    except WeChatAPIError as exc:
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=f"distribute: {exc}")
        logger.error("[%s] distribute: 账户 %s 凭据未配置：%s", job.job_id, account, exc)
        return False
    existing = db.get_distribution(job_pk, WECHAT_PLATFORM, account=account, lang=DEFAULT_LANG)
    thumb_media_id = _resolve_thumb_media_id(account, args)
    if not thumb_media_id and existing and existing.wechat_media_id:
        # 重投 PATCH：复用原草稿现有封面，省去手动配 thumb_media_id
        thumb_media_id = _existing_draft_thumb(client, existing.wechat_media_id)
    if not thumb_media_id:
        # 既没配封面也没旧草稿可借（首次发该账户草稿）→ 用文章首图传永久素材自动当封面
        thumb_media_id = _auto_cover_media_id(client, account, job, html)
    if not thumb_media_id:
        db.update_job_status(
            job_pk, JobStatus.FAILED,
            error_message=f"distribute: account '{account}' 缺封面 thumb_media_id",
        )
        logger.error(
            "[%s] distribute: 账户 %s 缺封面（设 WECHAT_%s_THUMB_MEDIA_ID；或确保原草稿在以复用其封面）",
            job.job_id, account, account.upper(),
        )
        return False

    html, n_figs = _apply_figures(html, job, client, account)
    leftover = len(find_image_placeholders(html))
    if n_figs or leftover:
        logger.info("[%s] 配图：替换 %d 张，剩 %d 个占位符未配（无对应图）", job.job_id, n_figs, leftover)

    # 拼接产品模块（line×platform 平台专属视觉块：文章链接 + 公众号名片卡 + 产品/二维码图）
    module = _load_product_module(job.line, WECHAT_PLATFORM)
    if module:
        html = html + module
        logger.info("[%s] 已拼接产品模块 %s-%s", job.job_id, job.line, WECHAT_PLATFORM)

    db.update_job_status(job_pk, JobStatus.PUBLISHING)
    payload = _build_article_payload(
        title=article.title or job.title_hint or job.job_id,
        digest=article.digest or "", content_html=html,
        author=_resolve_author(account, args),
        thumb_media_id=thumb_media_id,
    )
    try:
        if existing and existing.wechat_media_id:
            try:
                client.update_draft(existing.wechat_media_id, 0, payload)
                media_id = existing.wechat_media_id
                logger.info("[%s] PATCH wechat/%s media_id=%s", job.job_id, account, media_id)
            except WeChatAPIError as exc:
                if exc.errcode != 40007:  # 40007=media_id 失效（草稿已被删/过期）→ 重建；其它错抛出
                    raise
                logger.warning("[%s] 原草稿 media_id 失效(40007)，改为新建草稿", job.job_id)
                media_id = client.create_draft([payload])
                logger.info("[%s] POST wechat/%s media_id=%s（原草稿已删，重建）", job.job_id, account, media_id)
        else:
            media_id = client.create_draft([payload])
            logger.info("[%s] POST wechat/%s media_id=%s", job.job_id, account, media_id)
    except WeChatAPIError as exc:
        db.upsert_distribution(
            job_pk, WECHAT_PLATFORM, account=account, lang=DEFAULT_LANG,
            publish_status="failed", publish_error=str(exc), assembled_dir=article.content_dir,
        )
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=f"publish: {exc}")
        logger.error("[%s] publish failed: %s", job.job_id, exc)
        return False

    db.upsert_distribution(
        job_pk, WECHAT_PLATFORM, account=account, lang=DEFAULT_LANG,
        wechat_media_id=media_id, publish_status="published", publish_error=None,
        assembled_dir=article.content_dir,
    )
    db.update_job_status(job_pk, JobStatus.PUBLISHED)
    return True


def _safe_product_name(job: Job) -> str:
    """取产品显示名（用于正文夹带扫描）；取不到回空串（最佳努力，不阻断生成）。"""
    if not job.product:
        return ""
    try:
        return (load_product_by_id(PRODUCTS_DIR, job.product).name or "").strip()
    except Exception:
        return ""


def _resolve_wechat_account(job: Job) -> str:
    """从 line 配置取该线对应的公众号账户（extra.wechat_account）；取不到回 default。"""
    if not job.line:
        return "default"
    try:
        line = load_line_by_id(LINES_DIR, job.line)
    except LineLoadError:
        return "default"
    return str((line.extra or {}).get("wechat_account") or "default")


def _make_client_getter() -> Callable[[str], WeChatClient]:
    """返回按账户建并缓存 WeChatClient 的 getter —— token 按账户隔离（头号坑）。"""
    cache: Dict[str, WeChatClient] = {}

    def get_client(account: str) -> WeChatClient:
        if account not in cache:
            cache[account] = WeChatClient(account=account)
        return cache[account]

    return get_client


def _load_product_module(line: Optional[str], platform: str) -> str:
    """读取 (line, platform) 的产品模块 HTML，拼到正文尾；无则回空串（不强制每线都有）。

    模块是平台专属视觉块（公众号文章链接 + 公众号名片卡 + 产品/二维码大图），由人工在公众号
    编辑器做好、用 get_draft 抽出存档到 inputs/product_modules/{line}-{platform}.html。微信编辑器
    原样 HTML（含 mp-common-profile 名片卡、data-src 图片）经 create_draft 重提交可正常渲染。
    """
    if not line:
        return ""
    path = PROJECT_ROOT / "inputs" / "product_modules" / f"{line}-{platform}.html"
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _resolve_author(account: str, args: argparse.Namespace) -> str:
    """草稿作者署名（用公众号名）：WECHAT_{ACCOUNT}_AUTHOR > --placeholder-author > DEFAULT_AUTHOR。"""
    return (
        os.getenv(f"WECHAT_{account.upper()}_AUTHOR", "").strip()
        or (getattr(args, "placeholder_author", "") or "").strip()
        or os.getenv("DEFAULT_AUTHOR", "").strip()
    )


def _resolve_thumb_media_id(account: str, args: argparse.Namespace) -> str:
    """该账户封面素材 id：WECHAT_{ACCOUNT}_THUMB_MEDIA_ID > --placeholder-thumb-media > DEFAULT。"""
    return (
        os.getenv(f"WECHAT_{account.upper()}_THUMB_MEDIA_ID", "").strip()
        or (getattr(args, "placeholder_thumb_media", "") or "").strip()
        or os.getenv("DEFAULT_THUMB_MEDIA_ID", "").strip()
    )


def _existing_draft_thumb(client: WeChatClient, media_id: str) -> str:
    """取已存草稿当前封面 thumb_media_id（重投 PATCH 时复用）；取不到回空串。"""
    try:
        data = client.get_draft(media_id)
    except WeChatAPIError:
        return ""
    items = data.get("news_item") or []
    if items:
        return str(items[0].get("thumb_media_id") or "").strip()
    return ""


def _fig_max_pages(job: Job) -> Optional[int]:
    raw = (job.extra or {}).get("max_pages")
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _resolve_figure_path(description: str, figures_dir: Path, extracted) -> Optional[str]:
    """占位符 → 图片本地路径：优先人工放的 figures/fig{N}.{png,jpg}，否则自动抽取的匹配图。"""
    num = figure_number(description)
    if num:
        for ext in (".png", ".jpg", ".jpeg"):
            p = figures_dir / f"fig{num}{ext}"
            if p.exists():
                return str(p)
    fig = match_figure(extracted, description)
    return fig.image_path if fig else None


def _upload_cached(client: WeChatClient, account: str, local_path: str) -> str:
    """上传图片到该账户，sha256 缓存避免重复上传（公众号 uploadimg 5000/日配额）。"""
    manifest_path = RUNTIME_DIR / f"image_upload_manifest_{account}.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        manifest = {}
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    if sha in manifest:
        return str(manifest[sha])
    url = client.upload_image(local_path)
    manifest[sha] = url
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return url


def _upload_cover_cached(client: WeChatClient, account: str, local_path: str) -> str:
    """上传封面到永久素材（material/add）拿 media_id，sha256 缓存避免重复（永久素材有数量上限）。"""
    manifest_path = RUNTIME_DIR / f"cover_material_manifest_{account}.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        manifest = {}
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    if sha in manifest:
        return str(manifest[sha])
    media_id = client.add_permanent_material(local_path, "image")
    manifest[sha] = media_id
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return media_id


def _load_pool_figures(job: Job) -> List[Figure]:
    """从 image_pool 的 figures_manifest.json 载入预备配图（按图号匹配 ``[图片:Figure N]``）。

    image_pool 是人工/预抽取放好的配图目录，优先于 PDF 自动抽取。caption 里的 FIG{N}
    解析出图号喂 match_figure。无 pool / 无 manifest / 解析失败都回 []（由 PDF 抽取兜底）。
    """
    if not job.image_pool:
        return []
    pool = PROJECT_ROOT / "inputs" / "image_pools" / job.image_pool
    manifest = pool / "figures_manifest.json"
    if not manifest.exists():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("[%s] image_pool manifest 解析失败：%s", job.job_id, exc)
        return []
    figs: List[Figure] = []
    for item in data.get("figures", []):
        rel = str(item.get("path") or "").replace("\\", "/")
        if not rel:
            continue
        p = PROJECT_ROOT / rel
        if not p.exists():
            p = Path(rel)  # path 可能已是绝对路径
        if not p.exists():
            logger.warning("[%s] image_pool 图缺失：%s", job.job_id, rel)
            continue
        caption = str(item.get("caption") or "")
        figs.append(Figure(
            label=figure_number(caption), is_extended=False, caption=caption,
            page=int(item.get("page") or 0), image_path=str(p),
            width=int(item.get("width") or 0), height=int(item.get("height") or 0),
        ))
    return figs


def _resolve_job_figures(job: Job) -> Tuple[List[Figure], Path]:
    """该 job 的配图来源：image_pool 能按图号匹配则优先，否则回落 PDF 自动抽取。

    关键：pool 的 figures_manifest 若 caption 没有 FIG{N}（图号解析为空，如 AAV 池
    caption 全空），这些图无法和正文 ``[图片:Figure N]`` 占位符匹配 —— 这时必须回落到
    带图号的 PDF 抽取图，否则正文会漏掉所有配图。
    """
    figures_dir = Path(ARTICLE_CONTENT_DIR) / job.job_id / "figures"
    pool_figs = _load_pool_figures(job)
    if any(f.label for f in pool_figs):  # pool 里至少有一张能按图号匹配才用 pool
        return pool_figs, figures_dir
    # VLM 视觉抽图：渲染含图页 → 模型给图号+整图 bbox（密集多面板图也能按图号抽，启发式做不到）
    try:
        from utils.vision_figures import extract_figures_via_vision, vision_enabled
        if vision_enabled() and job.pdf and Path(job.pdf).exists():
            vfigs = extract_figures_via_vision(job.pdf, str(figures_dir), max_pages=_fig_max_pages(job))
            if vfigs:
                return vfigs, figures_dir
    except Exception as exc:  # noqa: BLE001 - VLM 失败回落启发式
        logger.warning("[%s] VLM 抽图失败，回落启发式：%s", job.job_id, exc)
    if job.pdf and Path(job.pdf).exists():
        try:
            return extract_figures(job.pdf, str(figures_dir), max_pages=_fig_max_pages(job)), figures_dir
        except Exception as exc:  # noqa: BLE001 - 抽图失败不阻断投放
            logger.warning("[%s] 抽图失败：%s", job.job_id, exc)
    return [], figures_dir


def _render_pdf_cover(job: Job, figures_dir: Path) -> Optional[str]:
    """兜底封面：把 PDF 首页渲染成 PNG（期刊/综述 PDF 抽不到插图时用）。取不到回 None。

    这些来源 PDF 常无 Nature 式题注、抽图为 0，又没配固定封面 —— 渲染首页保证有封面可发。
    首页截图当封面只是占位，建议给该账户设固定品牌封面（WECHAT_{ACCOUNT}_THUMB_MEDIA_ID 优先）。
    """
    if not (job.pdf and Path(job.pdf).exists()):
        return None
    try:
        import pypdfium2 as pdfium  # 已是依赖（pdf_figure_extractor 用）
        doc = pdfium.PdfDocument(job.pdf)
        try:
            pil = doc[0].render(scale=2.0).to_pil()
        finally:
            doc.close()
        figures_dir.mkdir(parents=True, exist_ok=True)
        out = figures_dir / "_cover_page1.png"
        pil.save(str(out))
        return str(out)
    except Exception as exc:  # noqa: BLE001 - 兜底失败不阻断（回落空串→上层报缺封面）
        logger.warning("[%s] 兜底封面（PDF 首页）渲染失败：%s", job.job_id, exc)
        return None


def _auto_cover_media_id(client: WeChatClient, account: str, job: Job, html: str) -> str:
    """首次发该账户草稿、又没配封面时：用文章首图自动当封面（image_pool 优先，PDF 抽取兜底）。

    取 html 里第一个 ``[图片:…]`` 占位符对应的图；取不到回落到首张可用配图。
    传永久素材拿 thumb media_id（sha 缓存）。任何环节取不到图就回空串，由上层报"缺封面"。
    """
    extracted, figures_dir = _resolve_job_figures(job)
    cover_path: Optional[str] = None
    for desc in find_image_placeholders(html):
        cover_path = _resolve_figure_path(desc, figures_dir, extracted)
        if cover_path:
            break
    if not cover_path and extracted:
        cover_path = extracted[0].image_path
    if not cover_path:
        cover_path = _render_pdf_cover(job, figures_dir)  # 兜底：渲染 PDF 首页当封面
    if not cover_path:
        logger.warning("[%s] 自动封面：没抽到可用图、PDF 首页也渲染失败", job.job_id)
        return ""
    try:
        media_id = _upload_cover_cached(client, account, cover_path)
    except WeChatAPIError as exc:
        logger.error("[%s] 自动封面：上传永久素材失败 %s", job.job_id, exc)
        return ""
    logger.info("[%s] 自动封面：%s -> thumb media_id=%s", job.job_id, Path(cover_path).name, media_id)
    return media_id


def _apply_figures(html: str, job: Job, client: WeChatClient, account: str) -> Tuple[str, int]:
    """把 [图片:Figure N …] 占位符替换为真实公众号图（人工放的 fig{N}.png 优先，自动抽取兜底）。"""
    placeholders = find_image_placeholders(html)
    if not placeholders:
        return html, 0
    extracted, figures_dir = _resolve_job_figures(job)
    used = 0
    used_paths: set = set()
    for desc in placeholders:
        path = _resolve_figure_path(desc, figures_dir, extracted)
        if not path:
            # 配不到图：删掉占位符，不在草稿里留 [图片:…] 方括号文字
            html = html.replace(f"[图片:{desc}]", "", 1)
            continue
        if path in used_paths:
            # 同一张图已用过（如 Figure 1 与 Figure 1e 都指向 Fig 1）→ 删掉重复占位符，不重复插图
            html = html.replace(f"[图片:{desc}]", "", 1)
            continue
        try:
            url = _upload_cached(client, account, path)
        except WeChatAPIError as exc:
            logger.warning("[%s] 配图上传失败 %s：%s", job.job_id, path, exc)
            continue
        html = replace_image_placeholder(html, desc, url)
        used_paths.add(path)
        used += 1
    return html, used


def _build_article_payload(
    title: str, digest: str, content_html: str,
    author: str, thumb_media_id: str,
) -> dict:
    """公众号 draft/add 单篇 article 的最小字段集。"""
    return {
        "title": title[:64] or "未命名",          # 公众号上限 64 字
        "author": author[:8] or "TarMart",      # 公众号上限 8 字
        "digest": digest[:120] or title[:120],   # 公众号上限 120 字
        "content": content_html,
        "content_source_url": "",
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }


if __name__ == "__main__":
    raise SystemExit(main())
