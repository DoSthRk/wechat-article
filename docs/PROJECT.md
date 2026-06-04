# 项目介绍：wechat-article

> 给新会话/新协作者的全景文档。读完这一份就能上手干活。
>
> ⚠️ 本文档描述的是**目标架构**（两线 × 三平台、内容/投放解耦）。当前代码仍停在
> Phase 0 骨架（单线、单公众号、AI 软广），正在按本文档重构。"现状 vs 目标"在第 8 节列清。

## 1. 项目要做什么

**一句话**：把 PDF 素材批量做成**科普文章**，再扇出到**多个平台**的草稿/博客（公众号不直接发布，留人工最后把关）。

**两条产品线**（业务上完全平行）：

| 线 | 内容主题 | 固定产品 | 公众号账户 |
|---|---|---|---|
| **AAV** | AAV / 基因递送相关科普 | AAV 系列 | AAV 公众号 |
| **Solidex** | 免疫 / Solidex 相关科普 | Solidex 系列 | 免疫客公众号 |

**两个正交的轴**（理解全项目的关键心智模型）：

```
                              投放层(3 平台)
                    ┌──────────┬──────────┬──────────┐
                    │ 公众号    │ genemedi │ LinkedIn │
                    │ (2 账户)  │ blog     │ (待调研) │
   内容层(2 线)     ├──────────┼──────────┼──────────┤
     AAV     ──────►│ 渲染+     │ 渲染+    │ 渲染+    │
                    │ AAV 模块  │ AAV 模块 │ AAV 模块 │
                    ├──────────┼──────────┼──────────┤
     Solidex ──────►│ 渲染+     │ 渲染+    │ 渲染+    │
                    │ Sol 模块  │ Sol 模块 │ Sol 模块 │
                    └──────────┴──────────┴──────────┘

   一份科普正文(内容层产出一次) ──fan-out──► N 个平台投放(各自包装产品模块)
```

**核心设计：内容与投放解耦**。一篇科普正文生成一次，扇出到多个平台；每个平台落地前再套上自己的固定产品模块。内容不关心投到哪，投放不关心内容怎么写出来。

**不做的事**：
- 公众号不直接发布（最后一步必须人工在后台点发）
- 不自动选题（选题由用户在任务清单显式指定）
- 不抓数据（PDF 由用户准备；未来*可能*演变成网页源自动抓取）
- **AI 不选产品**（见第 4.2 节，这是相对早期设计的重大改动）

## 2. 项目谱系（重要：理解为什么代码长这样）

本项目是 `D:\dev-project\target-running` 的**孪生项目**。target-running 是一个生物医药靶点 → 网站文章的批量生成系统，跑在生产上（47.102.223.198）。

target-running 经过半年迭代，把"多模型并行 + reviewer + healer + merger + AI 翻译 + 人工抽检闸 + dashboard 流水线"这套**内容质量基础设施**打磨得比较扎实了。wechat-article 复用其中**形状和机制**，但**业务逻辑全换**。

| 哪些模块从 target-running 直接复用（A 类）| 没改 |
|---|---|
| `utils/logger.py`、`utils/runtime_paths.py`、`utils/stage_runner.py`、`utils/moonshot_billing.py`、`utils/account_providers.py`、`config.py` | 6 个文件原样拷贝 |

| 哪些是 target-running 有、本项目要**移植**的 |
|---|
| 多模型并行（可选）、reviewer/healer/merger、markdown 健康度安全网、**AI 翻译流程**（genemedi 多语言直接复用，见 4.3）、Dashboard、人工抽检闸、cascade_reset + PATCH 重发 |

| 哪些 target-running 的逻辑**永远不要**移植到本项目 |
|---|
| `utils/drupal_client.py`（→ 用各平台自己的投放 adapter 替代）|
| `utils/feishu_bitable.py`（已废弃）|
| `utils/link_resolver.py`（`@@TARMART::xxx@@` 占位符是 target-running 私有的；本项目的"产品模块"是我们自己的机制，别照搬）|
| `utils/verifier.py` 中 UniProt / fact_finder / bio_fact_cache 那一坨（生物医药专属）|
| 任何"靶点 / target / gene / protein"命名 |

**所以**：参考 target-running 可以读 `D:\dev-project\target-running\`（worktree 在 `.claude\worktrees\epic-sanderson`），但**绝不修改 target-running 的任何文件**——它是独立的生产项目。

## 3. 整体架构（端到端流水线）

```
PDF(每条线不同;现人工上传 → 未来可能网页源自动抓取)
   │
   ▼
[生成]  纯科普正文 + 结尾一句自然软广(点本线固定产品)
   │    提示词 = 共通基底 + line 差异;两线篇幅/格式统一
   │    产出:1 份基准语言正文(平台无关、随线)
   ▼
[翻译]  可选,仅 genemedi {lang};移植 target-running AI 翻译;生成后独立阶段
   │
   ▼
[组装]  按 (line × platform) 取固定产品模块,拼到正文尾
   │    公众号→图(+二维码) / blog→图+外链 / 领英→…
   ▼
[投放]  (内容/投放解耦的下游;每个平台一个可插拔 adapter)
   ├─ 公众号 · AAV 账户        ┐ 两套 app_id/secret
   ├─ 公众号 · 免疫客账户       ┘ → token 缓存必须按账户隔离(见 7.1)
   ├─ genemedi blog({lang},待网站 post 接口)
   └─ LinkedIn(待调研)

  全程状态写 DB：lines → articles(基准正文) → distributions(平台×[lang])
```

## 4. 内容层（2 条线）

每条线 = `{PDF 来源, 提示词差异, 固定产品}` 三件套。

### 4.1 PDF 来源

- 两条线的 PDF 来源不同。
- 现状：**人工上传**到 `inputs/pdfs/{line}/`。
- 未来*可能*：从某个网页源**自动抓取**（届时加 `utils/{line}_fetcher.py`，不影响下游）。

### 4.2 产品植入（相对早期设计的重大改动）

**早期设计**：把产品信息塞进 prompt，让 AI "润物细无声"地软广融入正文。

**现在改成**：**AI 不碰选品**。正文几乎纯科普、不出现产品；只在**结尾一句**自然点名本线的**固定产品系列**。

**为什么解耦**（决策依据，别推翻）：
1. 产品信息细分后**太多**，塞不进也喂不准；
2. AI 选品**准确度不可控**。

→ **选品 100% 人工锁定**，写在 line 配置里。

**收尾那句怎么写（方案 B，已拍板）**：
- prompt 里告诉 AI "本线固定产品 = X，自然收个尾推荐它"；
- **AI 只负责把这句写得自然、贴合本篇主题；选品仍 100% 人工**；
- 准确度风险 ≈ 0（没有选择，只有措辞）；
- 这句收尾文字属于**基准正文**（随线、平台无关）；产品的**视觉模块**（图/外链/二维码）在组装阶段按平台叠加（见第 5 节）。

> 备选方案（未采用，记录备查）：A 全静态人工写死收尾文字、AI 完全不碰；C 正文只铺垫不点名、产品名全交组装阶段。最终选 B（AI 措辞 + 人工选品）。

### 4.3 提示词与篇幅

- 两条线的提示词**可提炼共通基底** + 每条线少量差异化：
  - `prompts/base.system.md` —— 共通基底（角色、科普原则、输出契约、图片占位符契约）
  - `prompts/line_aav.md` / `prompts/line_solidex.md` —— 每线差异（主题侧重、固定产品注入点、禁用词）
- 两条线的**篇幅和格式一致**，共用同一套约束。

### 4.4 翻译（独立阶段）

- genemedi 的 `{lang}` 多语言**不在生成里管**，而是**生成之后的独立 AI 翻译阶段**。
- 直接移植 target-running 那套 AI 翻译（已验证能完美适配）。
- 只有 genemedi 多语言落地时才触发；公众号/领英用基准语言。

## 5. 投放层（3 平台）

| 平台 | 形态 | 状态 |
|---|---|---|
| 公众号 ×2 | AAV 账户 + 免疫客账户（**两套 app_id/secret**） | Phase 0 已有单账户客户端，需扩多账户 |
| genemedi blog | `{lang}.genemedi.com`，走后台 post 脚本接口 | **等网站人员提供接口** |
| LinkedIn（领英） | — | **暂未调研** |

### 5.1 产品模块（内容/投放唯一的"跨界"接缝）

正文平台无关；但**产品推荐模块是 `line × platform` 双重参数化**的固定模板：

- 粒度 = `line × platform`：`aav-公众号`、`aav-blog`、`aav-领英`、`solidex-公众号`…… 每格一套。
- 三个平台各自**基本固定**，所以不需要动态生成——**发布前组装（把固定模块拼到正文尾）即可**。
- 模块拆两部分，正好对应"文字随线、其余随平台"：

| 部分 | 归属 | 内容 |
|---|---|---|
| 软广文字（结尾一句） | **随线**，平台无关 | 已在生成阶段产出（方案 B），进基准正文 |
| 视觉模块 | **随平台** | 公众号:系列图(+二维码) / blog:系列图+外链 / 领英:待定 |

> 产品模块的**具体字段 / 资产格式 / 组装接口**用户已明确"放到后面设计"。本文档先锁定
> 粒度（line × platform）和注入时机（投放前组装），schema 细节留 TODO。

## 6. 数据模型

```
line (1) ──< article 基准正文(1 PDF = 1 article) ──< translation(per lang,可选)
                       └──────────────────────────< distribution(platform [× lang])
                                                      组装后产物 + 发布状态
                                                      + media_id / url / 外链
```

- **line** 升级为一等概念（aav / solidex）。
- **article** = 基准语言科普正文（平台无关）。
- **distribution** = 一个 (article × platform [× lang]) 的落地实例，1 篇 article 对应 N 个 distribution（**1:N，相对 Phase 0 的 1:1 是结构性改动**）。
- 落盘/库分离（沿用 Phase 0 教训）：markdown / HTML / 译稿 / 组装成品落盘到 `outputs/`，DB 只存元数据 + 状态 + 路径，**绝不在 DB 里塞大文本**。

## 7. 关键约定（写代码前必看）

### 7.1 语言

- **注释、docstring、日志消息**：中文（保持跟 target-running 一致）
- **代码标识符（类名/函数名/变量名）**：英文 PEP 8
- **错误抛出的 message**：中文 OK，但保留英文术语（如 `markdown_unhealthy`）

### 7.2 类型 & 数据结构

- 公开 API（跨模块调用）函数签名加 type hints
- 数据传输用 `@dataclass`，跨模块共享的用 `frozen=True`（参考 `template_loader.StyleTemplate`）
- 不用 `*args, **kwargs` 作为公开接口（除非真的需要透传）

### 7.3 配置

- 一切外部配置走 `.env` + `os.getenv(KEY, default)`
- `.env` 里只有真实值；`.env.example` 是模板永远在 git 里
- 涉及凭据的环境变量**绝不打印到日志或 commit 里**
- **多账户/多平台凭据**：按 `{line}` / `{platform}` 命名空间隔离（如 `WECHAT_AAV_APP_ID` / `WECHAT_IMMUNE_APP_ID`）

### 7.4 落盘 vs 数据库

- **文件**：生成的 markdown / html / 译稿 / 组装成品落盘到 `outputs/`
- **数据库**：只存元数据 + 状态 + 引用路径，**绝不在 DB 里塞大文本**

### 7.5 错误处理

- LLM 调用、HTTP 请求、文件 I/O 这些容易失败的，**捕获异常封装成自定义 Error 类**（参考 `WeChatAPIError`、`PDFExtractError`、`TemplateLoadError`）
- worker 函数返回 `(bool, message)` 元组或 result dataclass，不抛错让上层判断
- 网络重试逻辑统一，**绝不无限重试**

### 7.6 测试

- 单元测试用 `unittest` / `pytest`（target-running 是 unittest）
- 网络层（公众号 API、LLM、blog 接口）全 mock
- 测试文件命名 `test_xxx.py`

## 8. 现状 vs 目标

### Phase 0 已完成（端到端骨架，但产品模型将被重构）

✅ **已有**：
- PDF 文本抽取（文字版 PDF；扫描件 OCR 留后续）
- 任务清单 / 风格模板 / 产品信息 YAML 加载
- 单 LLM 调用生成 markdown（DeepSeek，**目前是 AI 软广，将改为方案 B**）
- markdown → 公众号 HTML 转换（保留 `[图片:xxx]` 占位符）
- WeChat 单账户客户端：access_token 双层缓存、draft/add、draft/update（PATCH 重发）、media/uploadimg
- SQLite 状态跟踪（tasks / jobs / articles / article_drafts，**1:1，将扩为 line/article/distribution 1:N**）
- CLI：`--dry-run` / `--only`

### 与目标架构的主要差距（= TODO 的来源）

| 差距 | 方向 |
|---|---|
| 单线 → 双线 | `line` 升为一等概念，提示词拆基底+差异 |
| AI 软广 → 方案 B（人工选品 + AI 措辞） | 改 prompt + product 配置语义 |
| 生成即发布（耦合） → 内容/投放解耦 | batch_processor 拆 generate / distribute 两阶段 |
| article→draft 1:1 → article→distribution 1:N | DB 重塑 |
| 无产品模块 → line×platform 固定模块组装 | 新增组装阶段 |
| 单公众号 → 多账户 + blog + 领英 | 投放 adapter 抽象，token 按账户隔离 |
| 无翻译 → 生成后独立 AI 翻译 | 移植 target-running |
| 无质量网 / 无 dashboard | 移植安全网 + 矩阵看板 |

详见 `docs/TODO.md`（按新架构重排的 Phase 路线图）。

## 9. 文件布局（目标）

```
wechat-article/
├── README.md
├── docs/
│   ├── PROJECT.md          # ← 你正在读
│   └── TODO.md             # Phase 路线图（任务详情 + 验收）
├── .env.example
├── requirements.txt
├── config.py               # A 类复用
│
├── inputs/                 # 用户提供的素材（绝大多数 .gitignore）
│   ├── jobs.yaml           # 任务清单（每项含 line 字段）
│   ├── lines/              # 线配置（aav.yaml / solidex.yaml：PDF 来源、固定产品、提示词差异指针）
│   ├── pdfs/{line}/        # PDF（.gitignore）
│   └── product_modules/    # line×platform 产品模块配置 + 资产（图/二维码）
│
├── outputs/
│   ├── articles/{article_id}/   # 基准正文 article.md / .html / meta.json
│   ├── translations/{...}/      # {lang} 译稿
│   └── distributions/{...}/     # 各平台组装成品
│
├── runtime/                # .gitignore
│   ├── wechat_article.db
│   └── wechat_token_{account}.json   # 按账户隔离的 token 缓存
│
├── prompts/
│   ├── base.system.md      # 共通基底
│   ├── line_aav.md         # AAV 线差异
│   ├── line_solidex.md     # Solidex 线差异
│   ├── healer.system.md    # 质量网（移植）
│   ├── merger.system.md    # 多路合并（可选）
│   └── tonal_qa.system.md  # 科普调性自审
│
├── core/
│   └── main.py             # ArticleAnalyzer（生成基准正文）
│
├── db/
│   └── database.py         # line / article / translation / distribution
│
├── utils/
│   ├── logger.py / runtime_paths.py / stage_runner.py /
│   │  moonshot_billing.py / account_providers.py        # A 类复用
│   ├── pdf_extractor.py / job_loader.py / line_loader.py
│   ├── product_module_loader.py    # line×platform 模块
│   ├── assembler.py                # 正文 + 平台模块 → 成品
│   ├── translator.py               # 移植 target-running
│   ├── wechat_client.py            # 多账户
│   ├── wechat_html.py
│   ├── publishers/                 # 投放 adapter（wechat / blog / linkedin）
│   ├── tonal_qa.py / health_check.py / verifier.py
│
├── batch_processor.py      # 主入口（generate / distribute 两阶段）
├── app.py                  # Dashboard（后期）
└── tests/
```

## 10. 重要 gotcha

### 10.1 两个公众号 = access_token 缓存会撞车（新架构头号坑）

公众号 API 同一时刻每个账户只允许一个有效 access_token。两条线是**两套 app_id/secret**。
现有 `utils/wechat_client.py` 把 token 缓存到**单个** `runtime/wechat_token.json` —— 两账户会互相覆盖 token。

**必须**：token 缓存文件按账户隔离（`runtime/wechat_token_{account}.json`），`WeChatClient` 以 app_id 为缓存键。单机文件方案够用；多服务器要加分布式锁/Redis。

### 10.2 公众号 HTML 限制

- ❌ 不接受 `<script>` / `<iframe>` / `<style>` 块（已剥）
- ❌ 外链图片 `<img src="非mmbiz...">` 会被吞或转存（必须用 `mmbiz.qpic.cn` 域）
- ❌ `<a href="外链">` 在普通公众号会被吞 href（认证号可保留）—— **产品模块的外链/二维码要考虑这点**
- ✅ `<table>` 支持，但 border/padding 需内联到 `style=""`

`utils/wechat_html.py` 已处理表格/引用/代码块内联样式 + 危险标签剥离。

### 10.3 access_token 错误码识别

`40001`（无效）/ `42001`（过期）/ `40014`（不合法）都意味 token 失效。
`WeChatClient._maybe_invalidate_token()` 已处理 → 清缓存下次强刷。业务调用方也要识别 `WeChatAPIError.errcode` 决定是否重试。

### 10.4 PDF 解析的脆弱性

- 文字版 PDF：`pdfplumber` 一般稳
- 扫描版 PDF：拿不到文本，抛 `PDFExtractError`（OCR 是后续工作）
- 双栏/复杂版式：抽取顺序可能错乱

### 10.5 不同平台的产品模块不要硬编码

产品模块是 `line × platform` 配置驱动的（图/外链/二维码各平台不同）。**别把任何平台的模块写死在组装器里**，全走 `inputs/product_modules/` 配置。

### 10.6 Python 3.11+

代码用 PEP 585 内置泛型（`list[str]` 等），最低 3.10，推荐 3.11（target-running 服务器跑 3.11）。

## 11. 下一步看 `docs/TODO.md`

待办按新架构重排，每个任务带：涉及文件、验收标准、依赖关系。
