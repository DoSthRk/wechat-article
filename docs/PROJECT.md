# 项目介绍：wechat-article

> 给新会话/新协作者的全景文档。读完这一份就能上手干活。

## 1. 项目要做什么

**一句话**：批量驱动 —— 每个 PDF → 一篇公众号文章 → 推到公众号草稿箱（不直接发布，留人工最后把关）。

**典型用法**：用户在 `inputs/pdfs/` 放一批 PDF（业内论文 / 行业报告 / 厂商白皮书 / 自家产品规格书），在 `inputs/jobs.yaml` 里定义每个 PDF 对应哪个风格模板、哪个产品做软广。跑一行命令，全部文章生成完进公众号草稿箱，运营点开后台预览微调即发。

**不做的事**：
- 不直接发布（最后一步必须人工）
- 不自动选题（选题由用户在 jobs.yaml 显式指定）
- 不抓数据（PDF 由用户准备）

## 2. 项目谱系（重要：理解为什么代码长这样）

本项目是 `D:\dev-project\target-running` 的**孪生项目**。target-running 是一个生物医药靶点 → 网站文章的批量生成系统，跑在生产上（47.102.223.198）。

target-running 经过半年迭代，把"多模型并行 + reviewer + healer + merger + 人工抽检闸 + dashboard 流水线"这套**内容质量基础设施**打磨得比较扎实了。wechat-article 复用其中**形状和机制**，但**业务逻辑全换**。

| 哪些模块从 target-running 直接复用（A 类）| 没改 |
|---|---|
| `utils/logger.py`、`utils/runtime_paths.py`、`utils/stage_runner.py`、`utils/moonshot_billing.py`、`utils/account_providers.py`、`config.py` | 6 个文件原样拷贝 |

| 哪些是 Phase 0 全新写的 |
|---|
| PDF 解析 / 任务清单 / 模板 / 产品 loader、WeChat 客户端、markdown → 公众号 HTML 转换、ArticleAnalyzer、SQLite 表结构、batch_processor 主入口、文章写作 prompt |

| 哪些是 target-running 有、本项目**还没移植**（Phase 1-4 任务）|
|---|
| 多模型并行（3 路）、reviewer/healer/merger、markdown 健康度安全网、Dashboard、人工抽检闸、cascade_reset + PATCH 重发 |

| 哪些 target-running 的逻辑**永远不要**移植到本项目 |
|---|
| `utils/drupal_client.py`（→ 用 `wechat_client.py` 替代）|
| `utils/feishu_bitable.py`（已废弃）|
| `utils/link_resolver.py`（`@@TARMART::xxx@@` 占位符是 target-running 私有的）|
| `utils/verifier.py` 中 UniProt / fact_finder / bio_fact_cache 那一坨（生物医药专属）|
| 任何"靶点 / target / gene / protein"命名 |

**所以**：Codex 写新代码时如果要参考 target-running，可以读 `D:\dev-project\target-running\`（worktree 在 `.claude\worktrees\epic-sanderson`），但**绝不修改 target-running 的任何文件**——它是独立的生产项目。

## 3. 整体架构

```
                       inputs/jobs.yaml
                              │
                              ▼
                       ┌──────────────┐
                       │ job_loader   │  解析任务清单
                       └──────┬───────┘
                              │
       ┌──────────────────────┼──────────────────────┐
       ▼                      ▼                      ▼
  pdf_extractor      template_loader        product_loader
  (PDF→文本)          (风格 YAML)             (产品 YAML)
       │                      │                      │
       └──────────────────────┼──────────────────────┘
                              ▼
                    ┌──────────────────┐
                    │  ArticleAnalyzer │  Phase 0：单 LLM 调用
                    │  (core/main.py)  │  Phase 3：3 路并行 + merger
                    └────────┬─────────┘
                             │ markdown
                             ▼
                ┌────────────────────────┐
                │ tonal_qa（Phase 2 加）  │  调性自审 + 硬广扫描
                └────────────┬───────────┘
                             │
                             ▼
                ┌────────────────────────┐
                │  wechat_html           │  markdown → 公众号 HTML
                │  + image_provider      │  Phase 1 加：占位符替换
                └────────────┬───────────┘
                             │ HTML
                             ▼
                    ┌──────────────────┐
                    │  WeChatClient    │  draft/add 或 draft/update
                    │  (uploadimg 给图)│  (重发同 media_id 走 PATCH)
                    └────────┬─────────┘
                             │
                             ▼
                       公众号草稿箱
                       （人工审阅后发）

  全程状态写 SQLite：tasks → jobs → articles → article_drafts
```

## 4. 文件布局

```
wechat-article/
├── README.md               # 5 分钟上手
├── docs/
│   ├── PROJECT.md          # ← 你正在读
│   └── TODO.md             # Phase 1-4 待办（任务详情）
├── .env.example            # 环境变量样例
├── .gitignore
├── requirements.txt        # Phase 0 依赖
├── config.py               # 复用自 target-running，DeepSeek API key 读取
│
├── inputs/                 # 用户提供的素材（绝大多数 .gitignore）
│   ├── jobs.yaml           # 实际任务清单
│   ├── jobs.example.yaml   # 模板（入库）
│   ├── style_templates/    # 风格 YAML
│   │   ├── *.yaml          # 实际（.gitignore）
│   │   └── *.example.yaml  # 模板（入库）
│   ├── products/           # 产品 YAML
│   ├── pdfs/               # PDF（.gitignore）
│   └── image_pools/        # 图库（.gitignore，Phase 1 用）
│
├── outputs/jobs/{job_id}/  # 落盘产物
│   ├── article.md          # LLM 出的 markdown
│   ├── article.html        # 转出的公众号 HTML
│   ├── meta.json           # 标题/摘要/token/时间戳
│   └── images_used.json    # Phase 1 加：用了哪些图、对应 mmbiz URL
│
├── runtime/                # .gitignore
│   ├── wechat_article.db   # SQLite 状态
│   └── wechat_token.json   # access_token 缓存
│
├── prompts/
│   ├── article_writer.system.md  # 主写作（学术中立 + 软广不硬塞）
│   ├── healer.system.md          # Phase 3 加：审核/修复
│   ├── merger.system.md          # Phase 3 加：三稿合并
│   └── tonal_qa.system.md        # Phase 2 加：调性评分
│
├── core/
│   └── main.py             # ArticleAnalyzer
│
├── db/
│   └── database.py         # 4 张表 + ORM；Phase 1+ 按需加列
│
├── utils/
│   ├── logger.py / runtime_paths.py / stage_runner.py /
│   │  moonshot_billing.py / account_providers.py    # ← A 类复用
│   ├── pdf_extractor.py
│   ├── job_loader.py
│   ├── template_loader.py
│   ├── product_loader.py
│   ├── wechat_client.py    # access_token + draft + uploadimg
│   ├── wechat_html.py      # md → HTML + 占位符工具
│   ├── image_provider.py   # Phase 1 加：抽象接口
│   ├── wechat_image_provider.py  # Phase 1 加：具体实现
│   ├── tonal_qa.py         # Phase 2 加
│   └── verifier.py         # Phase 3 加：从 target-running 改造移植
│
├── batch_processor.py      # 主入口（CLI）
├── app.py                  # Phase 4 加：Flask dashboard
├── templates/              # Flask 模板（Phase 4 用）
├── static/                 # 前端资源（Phase 4 用）
├── scripts/                # 辅助脚本（preflight、preupload_images 等）
└── tests/
```

## 5. Phase 0 完成的功能

✅ **覆盖**：

- PDF 文本抽取（文字版 PDF；扫描件 + OCR 留给后续）
- 任务清单加载（`jobs.yaml`，校验 job_id 唯一 + 引用文件存在）
- 风格模板加载（YAML 中的字数/章节/调性/禁用词全部喂给 prompt）
- 产品信息加载（卖点/规格/忌讳全部喂给 prompt 让 LLM 自然融入）
- 文章写作（单 LLM 调用，DeepSeek-V4-flash，输出 markdown）
- markdown → 公众号 HTML 转换（保留 `[图片:xxx]` 占位符原样不动）
- WeChat 客户端：access_token 内存+文件双层缓存（跨进程共享，避免 token 互踩）、`draft/add`（新建草稿）、`draft/update`（重发同 media_id 走 PATCH 不创建新草稿）、`media/uploadimg`（图片上传，Phase 1 才调）
- SQLite 状态跟踪（tasks / jobs / articles / article_drafts）
- 主入口 CLI：`--dry-run` 只生成不发；`--only job_id` 只跑指定 job；自动按 (task_id, job_id) upsert

❌ **不覆盖**（Phase 1-4 范围）：

- 图片自动选 + 上传 + 占位符替换
- 调性自审（学术中立度评分、硬广词黑名单扫描）
- 多模型并行 + reviewer + merger 整套质量机制
- markdown 健康度安全网（拦截损坏稿）
- Web Dashboard（流水线进度可视化 / 人工抽检闸 / 重生成）

## 6. 关键约定（写代码前必看）

### 6.1 语言

- **注释、docstring、日志消息**：中文（保持跟 target-running 一致）
- **代码标识符（类名/函数名/变量名）**：英文 PEP 8
- **错误抛出的 message**：中文 OK，但保留英文术语（如 `markdown_unhealthy`）

### 6.2 类型 & 数据结构

- 公开 API（跨模块调用）函数签名加 type hints
- 数据传输用 `@dataclass`，跨模块共享的用 `frozen=True`（参考 `template_loader.StyleTemplate`）
- 不用 `*args, **kwargs` 作为公开接口（除非真的需要透传）

### 6.3 配置

- 一切外部配置走 `.env` + `os.getenv(KEY, default)`
- `.env` 里只有真实值；`.env.example` 是模板永远在 git 里
- 涉及凭据的环境变量**绝不打印到日志或 commit 里**（已有 `_token` 私有属性 + 文件权限 600 处理）

### 6.4 落盘 vs 数据库

- **文件**：生成的 markdown / html / meta.json 落盘到 `outputs/jobs/{job_id}/`
- **数据库**：只存元数据 + 状态 + 引用路径，**绝不在 DB 里塞大文本**（学习 target-running 的教训，他们把 raw_response 塞 DB 撑爆过）

### 6.5 错误处理

- LLM 调用、HTTP 请求、文件 I/O 这些容易失败的，**捕获异常封装成自定义 Error 类**（参考 `WeChatAPIError`、`PDFExtractError`、`TemplateLoadError`）
- worker 函数（`analyze_one_job`、`_publish_worker` 类）返回 `(bool, message)` 元组，不抛错让上层判断
- 网络重试逻辑统一通过 retry 库或显式 for-loop，**绝不无限重试**

### 6.6 测试

- 单元测试用 `unittest`（target-running 是这套）
- 网络层的测试都 mock（参考 target-running 的 `tests/test_drupal_client.py`）
- 测试文件命名 `test_xxx.py`
- 不写 e2e（Phase 4 dashboard 上线之后再考虑 Playwright）

## 7. 重要 gotcha

### 7.1 WeChat access_token 是全局共享资源

公众号 API 同一时刻只允许一个有效 access_token。如果两个进程（比如 batch_processor 和将来 dashboard）各自调 `/cgi-bin/token` 拿，新的会顶掉旧的，结果两边都不能稳定调用。

**已有方案**：`utils/wechat_client.py` 把 token 缓存到 `runtime/wechat_token.json`，多进程通过这个文件共享。但**这是单机方案**——如果将来部署到多服务器要加分布式锁或 Redis。

### 7.2 thumb_media_id 是 Phase 0 的坑

公众号 `draft/add` 接口的 `thumb_media_id`（封面图素材 id）**必填非空**。Phase 0 没有图片管线，所以**用户必须手动**：
1. 去 mp.weixin.qq.com 后台
2. 内容与互动 → 素材管理 → 永久素材 → 上传一张图
3. 拿到这张图的 media_id（在素材管理页面能复制）
4. 填到 `.env` 的 `DEFAULT_THUMB_MEDIA_ID`

Phase 1 image_provider 接管后这步就免了，会自动从 image_pool 选一张当封面 + 上传。

### 7.3 公众号 HTML 限制

- ❌ 不接受 `<script>` / `<iframe>` / `<style>` 块（已剥）
- ❌ 外链图片 `<img src="https://非mmbiz...">` 会被吞或转存（必须用 `mmbiz.qpic.cn` 域）
- ❌ `<a href="外链">` 在普通公众号会被吞 href（认证号可保留）
- ✅ `<table>` 支持，但 border/padding 需要内联到 `style=""`
- ✅ `<img>` 必须有 `src`，**没有 alt 也会被保留**

`utils/wechat_html.py` 已经处理了表格/引用/代码块的内联样式注入和危险标签剥离。

### 7.4 access_token 错误码识别

公众号有几个意味着"token 失效"的 errcode：
- `40001`：access_token 无效
- `42001`：access_token 已过期
- `40014`：不合法的 access_token

`WeChatClient._maybe_invalidate_token()` 已经处理这三个 → 清缓存、下次调用强刷。但**业务调用方**也要识别 `WeChatAPIError.errcode`，因为强刷之后仍然要决定要不要重试。

### 7.5 PDF 解析的脆弱性

- 文字版 PDF：`pdfplumber` 一般稳，能拿 95%+ 文本
- 扫描版 PDF：完全拿不到文本，会抛 `PDFExtractError("PDF extracted no text...")`
- 双栏 / 复杂版式：抽取顺序可能错乱（表格穿插进文字流）

Phase 0 假设输入是干净的文字版 PDF。如果用户遇到扫描件需要 OCR，那是 Phase 1+ 的新工作（推荐用 `pytesseract` 或调云端 OCR）。

### 7.6 Python 3.11+

代码用了 `list[str]`、`dict[str, Any]`、`tuple[str, float]` 这种 PEP 585 内置泛型语法，**最低 Python 3.10**，推荐 3.11。target-running 的服务器跑 3.11。

## 8. 下一步看 `docs/TODO.md`

待办按 Phase 1 / 2 / 3 / 4 拆好了，每个任务有：
- 涉及哪些文件
- 验收标准（什么状态算"做完"）
- 跟其它任务的依赖关系

建议执行顺序：**Phase 1 → Phase 2 → Phase 3 → Phase 4**，但 Phase 1/2/3 之间**有独立性**（不一定要严格串行），看团队人手而定。Phase 4 必须最后做（dashboard 要展示前三阶段的字段）。
