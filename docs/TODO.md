# TODO：wechat-article Phase 1-4 待办

> 配套阅读：`docs/PROJECT.md`（项目背景 + 架构 + 约定）。本文档是可执行任务清单，每项带验收标准。

## 阶段路线图

```
Phase 0 (已完成 ✅) ── 端到端骨架：1 PDF → 1 LLM → 1 markdown → 1 草稿
   │
   ├─→ Phase 1 (图片管线) ─────┐
   │                          │
   ├─→ Phase 2 (调性自审) ─────┼─→ Phase 4 (Dashboard) ← 最后做
   │                          │
   └─→ Phase 3 (多模型 + QA) ──┘

Phase 1/2/3 之间无强依赖，可独立推进；Phase 4 需要前面三阶段的字段已落库。
```

## 通用注意事项

- 修改前先读 `docs/PROJECT.md` 的第 6 节（约定）和第 7 节（gotchas）
- 跨阶段改 DB schema 时**用 ALTER TABLE 平滑迁移**（参考 target-running 的 `_ensure_translation_columns` 模式）
- 涉及公众号 API 的改动**先在 dev 公众号上测**，别直接打生产订阅号
- 改完跑 `python -m pytest tests/ -q` 确保现有测试不挂；**新加功能必须配单元测试**

---

# Phase 1：图片占位符 → 上传 → 替换（1-2 天）

## 目标

让 AI 在文中标的 `[图片:xxx描述]` 占位符在最终草稿里变成真实的公众号图片。建立**可替换的图片接口**，今天调 WeChat 接口，明天换 OSS 不动业务代码。

## 任务清单

### 1.1 抽象图片接口

**文件**：新建 `utils/image_provider.py`

**实现**：
```python
class ImageProvider(Protocol):
    def find_image(self, pool: str, query: str) -> Optional[str]:
        """从图库 pool 找一张匹配 query 的图，返回本地路径"""
    def upload_image(self, local_path: str) -> str:
        """上传图片，返回前台可用 URL"""
```

加一个简单实现 `LocalPoolProvider`：
- `find_image(pool, query)`：在 `inputs/image_pools/{pool}/` 目录下找文件名匹配 `query` 关键词的图（fuzzy match：拆 query 关键词分别在文件名里匹配，命中数最多的赢；平局取最近修改的）
- `upload_image(local_path)`：调 `WeChatClient.upload_image(local_path)`

**验收**：
- 单元测试覆盖：能找到匹配图、找不到时返回 None、空 pool 时返回 None
- 不连真实公众号 API（upload_image 用 mock）

### 1.2 image_pool manifest（避免重复上传）

**文件**：新建 `utils/image_provider.py` 里加 `LocalPoolProvider` 持久化层

公众号 `media/uploadimg` 有 **5000 张/日配额**。同一张图反复上传是浪费配额。

**实现**：
- 每次 `upload_image(local_path)` 成功后，把 `(sha256(local_path), mmbiz_url, uploaded_at)` 记到 `runtime/image_upload_manifest.json`
- 下次调 `upload_image` 先查 manifest，命中就直接返回缓存的 mmbiz_url
- mmbiz_url 永久有效，所以缓存无需过期

**验收**：
- 同一张图调 5 次 upload_image，实际只调微信 API 1 次
- 单元测试 mock WeChatClient，验证 call_count

### 1.3 改 ArticleAnalyzer 让 LLM 输出占位符

**文件**：`prompts/article_writer.system.md`（已经包含占位符契约，**可能需要加强**）

**实现**：
- 复查 prompt 里 `[图片:xxx]` 契约是否够强
- 加规则：**每篇 2-4 张图**（开头 1 张吸睛、中间 1-2 张帮助理解、结尾 0-1 张总结）
- 不要在表格行内 / 列表项内插占位符（会破坏 markdown 结构）

**验收**：
- 跑 3 个不同 PDF，每篇 markdown 都有 2-4 个 `[图片:xxx]`
- 占位符都在段落之间独立成行（不混在 inline 文本里）

### 1.4 batch_processor 集成图片替换

**文件**：`batch_processor.py` 的 `_run_one_job()`

**当前流程**：
```
markdown → html → create_draft
```

**改成**：
```
markdown → html
  → find_image_placeholders(html)
  → for 每个 placeholder:
       local_path = image_provider.find_image(job.image_pool, description)
       if local_path:
           mmbiz_url = image_provider.upload_image(local_path)
           html = replace_image_placeholder(html, description, mmbiz_url)
       else:
           logger.warning(占位符无匹配图，留原样)
  → create_draft(html)
```

**新增配置**：
- `job.image_pool` 字段（jobs.yaml 已有该字段，Phase 0 没用）
- 选不到图时的策略：默认留占位符（草稿里 `[图片:xxx]` 字面显示），可加 `--strict-images` flag 让选不到图就标记 publish_blocked

**额外**：用上的图记到 `outputs/jobs/{job_id}/images_used.json`：
```json
[
  {"placeholder": "基因测序仪示意图", "local_path": "...", "mmbiz_url": "...", "uploaded_at": "..."},
  ...
]
```

**验收**：
- 完整跑一个带 3 个占位符的 PDF 任务，草稿里看到 3 张图正常显示
- `images_used.json` 有 3 条记录
- 重复跑同一任务，配额数字（mp.weixin.qq.com 后台看）只涨 1 次（首次上传），第二次走 manifest 缓存

### 1.5 thumb_media_id 自动化（去掉 .env 手动配置）

**文件**：`batch_processor.py` + `image_provider.py`

**实现**：
- 从匹配到的第一张图（或封面池 `inputs/image_pools/{pool}/cover.jpg`）调 `upload_image` 拿 mmbiz_url
- 但 thumb 需要的是 `media_id`（永久素材 ID），不是 mmbiz_url！
- 用 `/cgi-bin/material/add_material?type=image` 上传永久素材拿 media_id（**这是新接口，要加到 wechat_client.py**）
- 同样要 manifest 缓存避免重复上传

**新增 WeChatClient 方法**：`upload_thumb_material(local_path) -> str`（返回 media_id）

**验收**：
- 删 `.env` 里 `DEFAULT_THUMB_MEDIA_ID`，跑 batch_processor 仍能正常出草稿
- 草稿的封面是从 image_pool 选的图

### 1.6 写脚本：批量预上传

**文件**：新建 `scripts/preupload_images.py`

公众号配额是 5000/日，生成时一边算一边传容易撞配额导致整个 batch 失败。

**实现**：
- 命令行：`python scripts/preupload_images.py --pool inputs/image_pools/gene_therapy`
- 扫描指定 pool 下所有图，逐个调 `upload_image` 写进 manifest
- 显示进度条 + 配额剩余预估

**验收**：
- 跑一遍后，batch_processor 真实跑生成时所有图都命中 manifest，0 次实时上传

---

# Phase 2：调性自审（1 天）

## 目标

杜绝"硬广文""营销腔"流到草稿箱。所有生成的文章必须经过调性扫描，不合格的标 `publish_blocked = True` 不发布。

## 任务清单

### 2.1 硬广词黑名单

**文件**：新建 `data/hard_ad_words.txt`

**内容样例**（用户可后续扩展）：
```
业界领先
首选品牌
市场第一
最佳选择
行业标杆
立刻购买
限时优惠
重磅推出
震撼发布
颠覆性
划时代
独家提供
全网最低
仅此一家
强力推荐
不可错过
惊呆了
刷屏
轰动
史诗级
```

**附加**：
- 每行一个词
- `#` 开头是注释
- 用户能在 jobs.yaml 或 template yaml 里补充临时黑名单

### 2.2 静态扫描器

**文件**：新建 `utils/tonal_qa.py`

**实现**：
```python
@dataclass(frozen=True)
class TonalScanResult:
    score: int                 # 0-100，越高越合格
    hard_ad_hits: list[str]    # 命中的硬广词
    suggestions: list[str]     # 改写建议
    blocked: bool              # score < THRESHOLD 时 True
```

**功能**：
- `scan_static(markdown, hard_ad_words)`：扫描硬广词命中数，每命中 1 个扣 10 分
- `scan_llm(markdown, template, product)`：调 LLM（用 deepseek-v4-flash，便宜）打 0-100 分 + 给改写建议
- `merge_scan_results(static, llm)`：综合两者得最终 score

**阈值**：`TONAL_BLOCKED_THRESHOLD` env 配置，默认 60

### 2.3 调性 LLM prompt

**文件**：新建 `prompts/tonal_qa.system.md`

**要求 prompt 干的事**：
- 输入：完整 markdown + 风格模板 + 产品信息
- 输出 JSON：
  ```json
  {
    "score": 75,
    "is_neutral": true,
    "hard_ad_hits": ["独家秘籍", "立刻..."],
    "off_template": ["首段是抒情而非数据驱动"],
    "suggestions": [
      "把'独家秘籍'改成'公开方法'",
      "首段加一组关键数据"
    ]
  }
  ```
- 评分标准：
  - 90-100：完全符合"学术中立、润物细无声"
  - 70-89：基本合格，有 1-2 处轻微问题
  - 50-69：有明显营销腔，需要修
  - < 50：硬广味重，不能发

### 2.4 markdown 健康度安全网（从 target-running 移植）

**文件**：`core/main.py`（或新建 `utils/health_check.py`）

target-running 已经把这套打磨完了，**直接移植**：
- `_markdown_health_score(md) -> int`
- 拒绝条件：以 `<` 开头（HTML 串入）/ 含 `===PART_` / 长度 < 500 / 没 H1 / H2 < 3

**源码位置**：`D:\dev-project\target-running\.claude\worktrees\epic-sanderson\core\main.py:1258-1306`

### 2.5 集成到 batch_processor

**文件**：`batch_processor.py` 的 `_run_one_job()`

**流程**：
```
markdown 出来后
  → markdown 健康度 < 30 → publish_blocked = True，跳过发布
  → 调 tonal_qa.scan() → score < threshold → publish_blocked = True，跳过发布
  → 否则正常发布
```

**DB 新增字段**（迁移走 `_ensure_xxx_columns` 模式）：
- `articles.tonal_score INTEGER`
- `articles.tonal_feedback TEXT`
- `articles.markdown_health_score INTEGER`
- `articles.publish_blocked BOOLEAN`
- `articles.block_reason TEXT`

**验收**：
- 故意写一段硬广（用 jobs.yaml 指定一篇产品广告型 PDF），看是否被拦
- 正常文章不被误杀
- 被拦的稿在 outputs 里仍能找到（方便人工 review + 改），只是没发到草稿箱

---

# Phase 3：多模型 + reviewer + merger（1-2 天）

## 目标

从 target-running 移植"3 路并行 + 各路 reviewer + merger 合并"的完整质量机制。**用 env 开关控制**：`MULTI_ROUTE_ENABLED=true` 才启用，默认关。

## 任务清单

### 3.1 路由配置 + 多模型并行

**文件**：`core/main.py` 加 RouteConfig 抽象 + 3 路调度

**参考实现**：`D:\dev-project\target-running\.claude\worktrees\epic-sanderson\core\main.py`

target-running 的 3 路：
- `native_kimi`（kimi-k2.6, moonshot 官方）
- `relay_kimi`（kimi-k2.5, qnaigc 中转）
- `relay_gemini`（gemini-3.1-pro, wellapi 中转）

**wechat-article 推荐 3 路**（中文文案优化的组合）：
- `route_a`：`deepseek-v4-flash`（DeepSeek 官方，稳定中文）
- `route_b`：`qwen3-max`（阿里通义千问，中文表达另一种风格）
- `route_c`：`kimi-k2.6`（Moonshot，长上下文，复述能力强）

每路有独立的 base_url / api_key / model / max_tokens / timeout。

**改动**：
- `core/main.py` 加 `ArticleAnalyzer._run_single_route(route, job, prompt)`
- 三路用 `concurrent.futures.ThreadPoolExecutor` 并发
- 每路独立重试 + 思考模式切换（参考 target-running 的实现）

### 3.2 路审核（reviewer）

**文件**：新建 `utils/verifier.py`（**简化版**，去掉 target-running 的 fact_finder 整段）

**移植 + 简化**：
- 类 `CandidateAuditResult`（数据结构）→ 保留
- 类 `OutputVerifier.audit_candidate(candidate)` → 保留，**但 prompt 全换**
- 删 `get_facts_for_target` / `_fetch_uniprot_*` / `FactFinderResult` / `_validate_fact_payload`（全是 UniProt）

**新 prompt** `prompts/healer.system.md`：
- 判断维度：
  1. 主旨是否偏离 PDF 核心论点（`off_topic`，类似 target-running 的 `target_mismatch`）
  2. 是否违反 template 约束（字数、章节数、禁用词、调性）
  3. 是否有事实错误（与 PDF 矛盾、捏造数据）
  4. 软广是否硬塞（产品名超过 3 次 / 硬广词命中）
- 输出 JSON：
  ```json
  {
    "is_valid": true,
    "off_topic": false,
    "quality_score": 85,
    "reasons": [...],
    "changes": [...],
    "healed_markdown": "..."   // 修过的版本
  }
  ```

### 3.3 三稿合并（merger）

**文件**：复用 `utils/verifier.py` 里加 `merge_valid_candidates()`

**移植 + 简化**：
- target-running 的 `merge_valid_candidates(target_name, fact_result, candidates)` → 改成 `merge_valid_candidates(pdf_summary, template, product, candidates)`
- 删 `fact_payload` 入参
- `MergeResult` 数据类去掉 `merged_chinese`（公众号本就中文）

**新 prompt** `prompts/merger.system.md`：
- 输入：3 个候选 markdown + template + product
- 输出 JSON：
  ```json
  {
    "merged_markdown": "...",   // 唯一权威
    "merge_rationale": "...",
    "dedupe_summary": "..."
  }
  ```
- **markdown 是权威**（沿用 target-running 上周打磨完的契约）
- merged_markdown 必须 ≥ 500 字符、不以 `<` 开头、有 H1+3 个 H2（否则验证失败回退单稿）

### 3.4 primary 选稿 + 安全网

**文件**：`core/main.py`

**直接移植**：
- `_pick_primary_candidate(candidates)`：按 `markdown 健康度 > quality_score > token_usage` 排序
- 合并失败时降级到 primary 单稿

**源码**：`D:\dev-project\target-running\.claude\worktrees\epic-sanderson\core\main.py:1786-1820`

### 3.5 DB 加候选稿表

**文件**：`db/database.py`

**新表 `candidate_articles`**：
```sql
- id PK
- job_pk FK -> jobs.id
- route VARCHAR(32)      -- 'route_a' / 'route_b' / 'route_c'
- selected BOOLEAN       -- 是否被 merger 采纳
- is_valid BOOLEAN       -- reviewer 判定
- off_topic BOOLEAN
- quality_score INTEGER
- markdown TEXT          -- 候选稿全文（注意：可能比较大，要考虑这里塞 DB 还是落盘）
- changes JSON
- reasons JSON
- prompt_tokens / completion_tokens / total_tokens / latency_ms
- model VARCHAR(64)
- created_at / updated_at
```

**注意**：参考 target-running 的教训，**candidate markdown 也落盘到 `outputs/jobs/{job_id}/candidates/{route}.md`**，DB 只存路径，避免单行几十 KB 撑爆。

### 3.6 集成到 batch_processor

**改动**：
- `ArticleAnalyzer.analyze(job)` 内部根据 `MULTI_ROUTE_ENABLED` 走单路 or 3 路
- 多路结果：每个候选 → reviewer → 若 ≥2 个 valid → merger；否则取 primary 单稿
- 候选结果都落盘 + 写 DB
- 最终 markdown 跟单路一样回到主流程（继续 tonal_qa → 发布）

**验收**：
- env 切到 true，单篇文章耗时 5-8 倍，但 reviewer score 显著高于单路
- 候选 `outputs/jobs/{job_id}/candidates/*.md` 全部落盘
- merger 失败时优雅降级到 primary
- 关掉开关，行为跟 Phase 0 完全一致（向后兼容）

---

# Phase 4：Dashboard + 人工抽检 + 重生成（2-3 天）

## 目标

复刻 target-running 的完整 dashboard 体验：浏览器看流水线、单篇/批量操作、人工抽检闸、重生成走 PATCH。

## 任务清单

### 4.1 Flask app 骨架

**文件**：复制 `D:\dev-project\target-running\.claude\worktrees\epic-sanderson\app.py` 改造

**改造点**：
- 删所有 `/api/feishu/*` 端点
- 删所有 `xlsx_targets` 引用
- `target_name` → `job_id` 全文件 sed
- 启动入口、路径、port 等保持

### 4.2 流水线表格

**端点**：`/api/workflow/articles?page=N&page_size=M`

**返回**：
```json
{
  "articles": [
    {
      "job_id": "...",
      "task_name": "...",
      "pdf_path": "...",
      "template_id": "...",
      "product_id": "...",
      "generation_status": "completed",
      "tonal_score": 85,
      "publish_status": "published",
      "wechat_url": "...",
      "wechat_media_id": "..."
    }
  ],
  "page": 1,
  "page_size": 50,
  "total": 123
}
```

**前端**：`templates/dashboard.html` + `static/dashboard.js` 复用 target-running 的 pipeline 表格组件

### 4.3 单文章预览

**新端点**：`/preview/{job_id}` 在浏览器里渲染 markdown

**来源**：参考 `D:\dev-project\target-running\.claude\worktrees\epic-sanderson\templates\markdown_preview.html`

### 4.4 重生成级联 + draft/update PATCH

**改 batch_processor / workflow**：
- 加 `db.cascade_reset_for_regen(job_pk)`：把 article + draft 状态全置 pending，**保留 wechat_media_id** 给 PATCH
- `_publish_worker` 读已有 `wechat_media_id`，有则调 `update_draft`（PATCH 同位置），无则 `create_draft`
- API 端点 `/api/workflow/generate/run` 接 target_ids 时自动 cascade_reset

**参考实现**：`D:\dev-project\target-running\.claude\worktrees\epic-sanderson\db\database.py` 的 `cascade_reset_for_regen()`、`utils\drupal_client.py` 的 PATCH 路径、`core\workflow.py` 的 `_publish_worker`

**关键**：跟 target-running 的设计完全一致 —— **URL 稳定**，旧外链 / SEO 不丢。

### 4.5 人工抽检闸

**端点**：`/api/workflow/gate`

**功能**：
- 生成 → 翻译/发布之间可以挂"待人工抽检"
- DB 加 `workflow_settings` 表（target-running 已有）存闸状态
- 闸开时：生成完毕的文章 `review_status = pending`，dashboard 上需手动放行才会进入发布段
- 闸关时：自动放行

### 4.6 连续模式 supervisor

**端点**：`/api/workflow/continuous/start` 和 `/stop`

**功能**：后台线程自动驱动 generate → publish 全链路，直到所有 pending 完成

**参考**：`D:\dev-project\target-running\.claude\worktrees\epic-sanderson\core\workflow.py` 的 supervisor 实现

### 4.7 余额 / 计费可视化

**端点**：`/api/accounts/overview` + `/api/pricing`

**实现**：复用 `utils/account_providers.py` 和 `utils/moonshot_billing.py`（已经在 Phase 0 拷贝过去了）

**验收**：dashboard 顶部能看到 DeepSeek / Moonshot / qnaigc 各账户余额

---

# 各阶段共通：测试 & 部署

## 测试策略

| Phase | 必须的测试 |
|---|---|
| 1 | `test_image_provider.py` (find + upload mock)、`test_wechat_html.py` 补充占位符替换 |
| 2 | `test_tonal_qa.py` (静态扫描 + LLM mock)、`test_health_check.py` 移植自 target-running |
| 3 | `test_verifier.py`、`test_main.py` 验证 3 路降级、`test_workflow.py` 多路集成 |
| 4 | `test_dashboard_api.py`、`test_cascade_reset.py`、e2e: Playwright + 本地 dev 公众号（可选） |

## 部署考虑（Phase 4 之后才相关）

target-running 用了 systemd + 多 release 目录 + symlink + 重启的部署套路：
- `/opt/wechat-article/releases/{TAG}/` + `/opt/wechat-article/current` symlink
- `/opt/wechat-article/shared/.env`（生产配置不入 git）
- `/opt/wechat-article/shared/logs/`
- `systemd unit: wechat-article.service`

参考 `D:\dev-project\target-running\Jenkinsfile` 的部署阶段定义。Jenkins 不一定要用，可以写一个 `scripts/deploy.sh` 手动跑。

---

# 不在范围内（但可能将来要做的）

- 公众号自动发布（`freepublish` 接口）—— 永远不要！必须人工
- 多公众号支持（一套 jobs 同时发到 N 个公众号）—— 大改 DB 结构
- 内容分发到知乎 / 头条号等其它平台 —— 别在本项目里做，另起项目
- AI 出封面图 / 插图（调 fal-ai / dreamina）—— 可作为 Phase 1 的可选 image_provider 实现
- OCR 扫描版 PDF —— 加新 `utils/ocr_extractor.py`，pdf_extractor 失败时回退
- 文章数据看板（PV/UV/分享）—— 公众号的统计 API 也开放，但不是优先级

---

# 给 Codex 的建议工作流

1. **第一次接手**：先读 `README.md` → `docs/PROJECT.md` → 本文件
2. **挑任务**：从 Phase 1 第一个未完成任务开始，按编号顺序
3. **写之前**：grep 一下相关文件已有逻辑（特别是 `utils/` 和 `core/`）
4. **写完后**：
   - 跑 `python -m pytest tests/ -q` 确保不挂老测试
   - 加 1-3 个针对性单元测试
   - 用真实小样例 dry-run 一次（PDF 真的能解析、yaml 真的能加载等）
5. **不确定的设计抉择**：参考 target-running 同名/类似模块的做法（路径在 PROJECT.md 第 2 节）
6. **commit 风格**：跟 target-running 一致 —— `feat: xxx` / `fix: xxx`，第一行简短、正文用中文说清楚动机和影响
