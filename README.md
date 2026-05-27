# wechat-article

批量驱动：每个 PDF → 一篇公众号文章 → 推到公众号草稿箱（不发布，人工最后把关）。

形状上是 [target-running](https://gitlab.com/genemedi/target-running) 的孪生项目，但内容生成是从 PDF 素材出发、输出端是公众号草稿。

> **想接手开发的看这两份文档**：
> - 📖 [`docs/PROJECT.md`](docs/PROJECT.md) — 项目背景、架构、约定、gotchas（**先读这个**）
> - ✅ [`docs/TODO.md`](docs/TODO.md) — Phase 1-4 待办，每项带文件位置 + 验收标准

## 项目阶段

| Phase | 范围 | 状态 |
|---|---|---|
| 0 | 端到端骨架：1 PDF → 1 LLM → 1 markdown → 1 草稿；无图、无 QA、无 dashboard | ✅ |
| 1 | 图片占位符 + 上传到公众号 mmbiz；图片接口做成可替换（OSS 备用） | ⏳ |
| 2 | 调性自审（学术中立 / 软广扫描） + markdown 健康度安全网 | ⏳ |
| 3 | 多模型并行 + reviewer + merger（从 target-running 移植） | ⏳ |
| 4 | Dashboard 可视化 + 人工抽检闸 + 重生成（draft/update PATCH） | ⏳ |

## 目录结构

```
wechat-article/
├── inputs/                  # 你提供的素材
│   ├── jobs.yaml            # 任务清单（每项 = 一篇文章）
│   ├── style_templates/     # 风格模板 YAML（长度/章节/调性约束）
│   ├── products/            # 产品信息 YAML（用作软广素材）
│   ├── pdfs/                # 核心素材 PDF
│   └── image_pools/         # 候选图库（Phase 1）
├── prompts/                 # LLM system prompts
├── core/                    # ArticleAnalyzer
├── db/                      # SQLite/MySQL ORM（4 张表）
├── utils/                   # PDF / WeChat / 模板加载等
├── outputs/jobs/{job_id}/   # 落盘：article.md / .html / meta.json
├── runtime/                 # SQLite + access_token 缓存
├── batch_processor.py       # 主入口
└── scripts/                 # 辅助脚本（后续阶段补）
```

## Phase 0 上手（5 分钟）

```bash
# 1. 进项目
cd D:/dev-project/wechat-article

# 2. 装依赖
python -m venv .venv
.venv/Scripts/activate         # Windows
# source .venv/bin/activate    # macOS/Linux
pip install -r requirements.txt

# 3. 配环境
cp .env.example .env
# 编辑 .env，填好：
#   DEEPSEEK_API_KEY
#   WECHAT_APP_ID / WECHAT_APP_SECRET
#   DEFAULT_THUMB_MEDIA_ID    ← Phase 0 需要：先去公众号后台手动传一张永久图素材，把它的 media_id 填这里

# 4. 准备示例输入
cp inputs/jobs.example.yaml inputs/jobs.yaml
cp inputs/style_templates/academic_review.example.yaml inputs/style_templates/academic_review.yaml
cp inputs/products/sample_product.example.yaml inputs/products/sample_product.yaml
# 把一个 PDF 放进 inputs/pdfs/sample.pdf

# 5. 试跑（不真发，只生成产物）
python batch_processor.py --dry-run

# 6. 真发到公众号草稿箱
python batch_processor.py
```

跑完后产物在 `outputs/jobs/{job_id}/`：
- `article.md` —— LLM 输出的 markdown
- `article.html` —— 转成公众号 HTML
- `meta.json` —— 标题/摘要/token 等元数据

公众号草稿在 mp.weixin.qq.com → 内容与互动 → 草稿箱 看。

## Phase 0 的边界

✅ **包含**：单 PDF 单稿生成、PDF 文本抽取、模板/产品 YAML 加载、markdown → 公众号 HTML 转换、access_token 缓存、草稿创建/更新（同一 media_id 走 update 不新建）。

❌ **不包含**：
- 图片管线（AI 在文中标 `[图片:xxx]` 占位符会原样留在 HTML，**Phase 1 才接管**）
- 调性自审 / 硬广词扫描（Phase 2）
- 3 路并行 / reviewer / merger（Phase 3）
- Web dashboard（Phase 4）

## 常见问题

**Q：access_token 怎么管理？**
A：客户端会拿一次 token 缓存到 `runtime/wechat_token.json`，2 小时内复用。多个进程共享同一文件，避免互相覆盖。

**Q：草稿重发会创建新的还是更新？**
A：DB 里 `article_drafts.wechat_media_id` 非空时走 `draft/update`（更新现有草稿，**位置不变**）；空时走 `draft/add`（新建）。这跟 target-running 的 Drupal PATCH 是同一思路。

**Q：thumb_media_id 必须自己手动准备一个？**
A：Phase 0 是。Phase 1 加图片管线后会自动从 image_pool 选一张当封面，自动上传。

## 与 target-running 的关系

`target-running` 是医药靶点 → 网站文章的生成系统，**直接复用**的模块：
`utils/logger.py`、`utils/runtime_paths.py`、`utils/stage_runner.py`、`utils/moonshot_billing.py`、`utils/account_providers.py`、`config.py`。

**结构借鉴**（Phase 3 之后会移植代码）：3 路并行调度、reviewer/healer/merger 链路、markdown_health_score 安全网、cascade_reset_for_regen、draft PATCH 思路。

不复用：`drupal_client` / `feishu_bitable` / `link_resolver` / `verifier.py` 的 UniProt 部分 / 所有 target / 靶点命名。
