# 工作流程图：wechat-article

> 端到端流水线的全景。配套阅读 `docs/PROJECT.md`（架构与约定）、`docs/TODO.md`（路线图）。
>
> 核心心智模型：**内容与投放解耦**。内容生成一次（基准中文正文），按平台扇出投放；
> 公众号用中文原文，blog 按目标语翻译。

## 1. 三阶段总览

```mermaid
flowchart TD
  subgraph IN[输入 inputs/]
    JOBS["jobs.yaml<br/>job = line + pdf + template + product"]
    LINE["lines/{line}.yaml<br/>aav · solidex（固定产品 / 写作侧重）"]
    PDF["pdfs/*.pdf（事实地基 + 配图来源）"]
  end

  subgraph GEN["① generate 阶段　batch_processor --stage generate"]
    A2["拼 system：base.system.md + lines/{line}.md<br/>方案B：正文纯科普 · 结尾一句点名固定产品"]
    A3["extract_text(pdf, max_pages)"]
    A4["LLM deepseek-chat → markdown"]
    A5["落盘 article.md / article.html / meta.json"]
    QA{"质量自审<br/>health + 调性 + 正文夹带产品"}
  end

  ART[("articles 表<br/>+ 健康/调性/publish_blocked")]

  subgraph TR["② translate 阶段（仅 blog）　scripts/run_translation.py"]
    T1["translate_markdown 中文 → en/ja/ko/ru"]
    T2["translations/{lang}.md"]
  end

  subgraph DIST["③ distribute 阶段　batch_processor --stage distribute"]
    D1{"publish_blocked?"}
    D2["account = line.wechat_account"]
    D3{"该 distribution 已有 media_id?"}
    D4["update_draft（PATCH，位置不变）"]
    D5["create_draft（新建草稿）"]
    DDB[("distributions 表<br/>platform × account × lang")]
  end

  subgraph OUT[投放平台]
    WX["公众号草稿箱<br/>AAV / 免疫客<br/>（人工最后把关发布）"]
    BLOG["genemedi blog {lang}<br/>（待 Phase 4 adapter）"]
    LI["LinkedIn（待 Phase 4 / 调研）"]
  end

  DASH["app.py Dashboard<br/>内容 × 投放矩阵 + 质量 + 预览"]

  JOBS --> A2
  LINE --> A2
  PDF --> A3
  A2 --> A4
  A3 --> A4
  A4 --> A5 --> QA
  QA -->|通过 / 不通过都落库| ART
  ART --> D1
  ART -. 仅 blog 线 .-> T1 --> T2 -. 组装时取 .-> BLOG
  D1 -->|blocked| STOP["跳过投放（留人工 review）"]
  D1 -->|ok| D2 --> D3
  D3 -->|是| D4 --> DDB
  D3 -->|否| D5 --> DDB
  DDB --> WX
  ART --> DASH
  DDB --> DASH
```

## 2. 数据模型（1 篇基准正文 → N 个平台投放）

```mermaid
erDiagram
  tasks   ||--o{ jobs          : "一次运行 = 一个 task"
  jobs    ||--o| articles      : "1:1 基准正文（平台无关）"
  jobs    ||--o{ distributions : "1:N 平台投放实例"
  jobs    ||--o| article_drafts: "legacy（被 distributions 取代）"

  articles {
    string title
    string content_dir "outputs/jobs/{job_id}/"
    int    markdown_health_score
    int    tonal_score
    bool   publish_blocked
    text   block_reason
  }
  distributions {
    string platform "wechat / blog / linkedin"
    string account  "aav / immune / ..."
    string lang     "zh / en / ja / ko / ru"
    string publish_status "pending/publishing/published/blocked/failed"
    string wechat_media_id "重发走 PATCH 的关键"
    string external_url    "blog 外链"
  }
```

> 翻译产物目前**落盘**在 `outputs/jobs/{job_id}/translations/{lang}.md`（暂无独立表；
> 待 Phase 4 blog distribute 落地时再决定是否入 `translations` 表）。

## 3. 关键决策点

| 决策点 | 规则 |
|---|---|
| **方案 B 产品植入** | 正文纯科普、零产品；只在结尾一段自然点名本线固定产品一次（人工选品，AI 只措辞） |
| **质量闸** | `health < 30`（损坏稿）/ `tonal < 60`（硬广腔）/ **正文夹带产品** → `publish_blocked`，落盘留人工、不投放 |
| **翻译触发** | **仅 blog 链路**：中文基准正文 → en/ja/ko/ru；**公众号链路用中文原文，不翻译** |
| **公众号重发** | 同 `(job, wechat, account, zh)` 已有 `media_id` → `draft/update`（PATCH，草稿位置不变）；否则 `draft/add` |
| **大 PDF** | `job.extra.max_pages` / env `PDF_MAX_PAGES` 截前 N 页，防爆上下文 |

## 4. CLI 速查

```bash
# 生成（方案B 出基准正文 + 质量自审），不投放
python batch_processor.py --stage generate

# 翻译（仅 blog 需要）：中文基准 → 目标语
python scripts/run_translation.py --job <job_id> --langs en,ja

# 投放到公众号草稿（被质量闸拦的会自动跳过）
python batch_processor.py --stage distribute

# 一条龙（生成 + 投放）；--dry-run 只生成不投放
python batch_processor.py            # = --stage all
python batch_processor.py --dry-run

# 看板
python app.py                        # http://127.0.0.1:5000
```

## 5. 模块地图

```
inputs/         jobs.yaml · lines/ · pdfs/ · style_templates/ · products/
prompts/        base.system.md（方案B 基底）· lines/{aav,solidex}.md · translation.system.md
data/           hard_ad_words.txt · translation_glossary.csv · do_not_translate.txt
core/main.py    ArticleAnalyzer（拼 prompt → LLM → markdown）
utils/          pdf_extractor · job_loader · line_loader · template_loader · product_loader
                health_check · tonal_qa · translator · wechat_html · wechat_client
db/database.py  tasks / jobs / articles / distributions（+ article_drafts legacy）
batch_processor.py  generate / distribute 两阶段编排
scripts/run_translation.py  翻译 CLI（blog）
app.py + templates/ + static/  服务端渲染看板（vanilla，无 React）
outputs/jobs/{job_id}/  article.md/.html · meta.json · translations/{lang}.md
```

## 6. 实现状态

| 阶段 | 状态 |
|---|---|
| P1' 内容层方案B（AAV + Solidex 双线） | ✅ 已做 + 真跑验证 |
| P2 数据解耦（article→distribution 1:N）+ generate/distribute 两段 | ✅ 已做 |
| P5 翻译（中文源 → en/ja/ko/ru，blog 专用） | ✅ 已做 + 真译验证 |
| P6 质量安全网（health + 调性 + 正文夹带产品 + 闸） | ✅ 已做 |
| P7 Dashboard（内容×投放矩阵 + 预览） | ✅ 已做 |
| **P3 产品模块** `line×platform`（图/外链/二维码组装） | ⏳ 待定（需先敲 schema） |
| **P4 多平台投放 adapter**（双公众号 token 隔离 / blog 接口 / LinkedIn） | ⏳ 待定 |

> P3/P4 待定中。当前 distribute 只接公众号单账户；blog/LinkedIn 投放 adapter 与
> 产品模块组装是接下来两块（参见 `docs/TODO.md`）。
```
