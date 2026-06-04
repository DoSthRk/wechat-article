# TODO：wechat-article 路线图（两线 × 三平台 · 内容/投放解耦）

> 配套阅读：`docs/PROJECT.md`（项目背景 + 架构 + 约定 + gotchas）。本文档是可执行任务清单。
>
> ⚠️ 本路线图已按**目标架构**重排，替换了早期"单线 / 单公众号 / AI 软广"那版。
> 当前代码 = Phase 0 骨架，正按 Phase 1 起重构。

## 阶段路线图

```
Phase 0 (已完成 ✅) ── 端到端骨架：1 PDF → 1 LLM → 1 markdown → 1 公众号草稿
   │                  （但产品模型是 AI 软广、DB 是 1:1，将在下面被改写）
   ▼
Phase 1 (内容层重构) ── 双线 + 去 AI 选品 + 软广方案 B + 提示词基底/差异拆分
   │
   ▼
Phase 2 (数据模型解耦) ── line/article/distribution(1:N) + generate/distribute 两阶段
   │
   ├─→ Phase 3 (产品模块组装 line×platform) ──┐
   ├─→ Phase 4 (多平台投放 adapter) ──────────┤
   ├─→ Phase 5 (AI 翻译阶段，移植) ───────────┼─→ Phase 7 (Dashboard) ← 最后做
   │                                          │
   └─→ Phase 6 (质量安全网) ──────────────────┘

依赖：Phase 1 → 2 是硬前置；3/4/5/6 在 2 之后相对独立可并行；7 需要前面字段已落库。
```

## 通用注意事项

- 修改前先读 `docs/PROJECT.md` 第 7 节（约定）和第 10 节（gotchas）
- 跨阶段改 DB schema **用 ALTER TABLE 平滑迁移**（参考 target-running 的 `_ensure_xxx_columns` 模式）
- 涉及公众号 API 的改动**先在 dev 公众号上测**
- 改完跑 `python -m pytest tests/ -q`；**新功能必须配单元测试**，网络层全 mock

---

# Phase 1：内容层重构（双线 + 去 AI 选品 + 软广 B）

## 目标

把"单线 + AI 软广"重构成"双线 + 纯科普正文 + 结尾方案 B 软广"。这是后续所有阶段的地基。

## 任务清单

### 1.1 `line` 升为一等概念 + 线配置

**文件**：新建 `inputs/lines/aav.yaml`、`inputs/lines/solidex.yaml`；新建 `utils/line_loader.py`

**线配置内容**（人工维护）：
```yaml
line_id: aav
name: "AAV 线"
pdf_source: inputs/pdfs/aav/        # 现人工上传；未来可换抓取器
prompt_overlay: line_aav            # 指向 prompts/line_aav.md
fixed_product:                      # 选品 100% 人工锁定（见 PROJECT 4.2）
  name: "GeneMedi AAV 系列"
  series: "AAV"
  closing_hint: "本线结尾自然点名的角度提示（喂给 AI 措辞，AI 不选品）"
forbidden_phrases: [...]            # 本线禁用词
```

**实现**：`load_line(line_id) -> Line`（frozen dataclass），校验引用文件存在。

**验收**：能加载两条线配置；缺字段抛 `LineLoadError`；单元测试覆盖。

### 1.2 提示词拆共通基底 + 线差异

**文件**：`prompts/base.system.md`（共通基底）+ `prompts/line_aav.md` / `prompts/line_solidex.md`（差异）；`core/main.py` 加组装逻辑

**实现**：
- `base.system.md`：角色、科普中立原则、输出契约、图片占位符契约（两线共用）
- 线差异文件：主题侧重、固定产品注入点、禁用词
- 组装：`system_prompt = base + "\n\n" + line_overlay`

**验收**：两条线生成的 system prompt 都正确包含各自差异段；篇幅/格式约束两线一致。

### 1.3 改写写作 prompt 走方案 B

**文件**：`prompts/base.system.md` + 各线 overlay

**实现**：
- 删掉早期"软广润物细无声融入正文 ≤3 次"那套
- 新规则：
  1. **正文纯科普，不出现任何产品名/品牌**
  2. **仅结尾一句**自然点名本线固定产品（产品名由程序注入，AI 只负责把这句写得自然、贴合本篇主题）
  3. 选品是程序给定的，**AI 不得改换产品**
- `_build_user_message` 注入 `line.fixed_product`（name + closing_hint）

**验收**：
- 跑两线各 3 个 PDF：正文无产品、结尾恰好自然点名各自固定产品 1 处
- 故意换 PDF 主题，结尾措辞会变（贴主题），但产品名不变（人工锁定生效）

### 1.4 product 语义从"AI 软广素材"改为"固定产品配置"

**文件**：`utils/product_loader.py` → 并入 line 配置或改造

**实现**：
- 早期 `product_loader` 是把卖点/规格喂 AI 软广 —— 这套生成期用法废弃
- 产品信息现在分两处用：
  1. 生成期：只用 `fixed_product.name` + `closing_hint`（1.3 已接）
  2. 投放期：完整产品资产（图/外链/二维码）进 `product_modules`（Phase 3）
- 清理/迁移 `product_loader`，避免遗留的"软广融入"路径

**验收**：grep 确认生成路径不再把产品卖点整段喂 prompt；旧 `product.to_prompt_block()` 软广用法已移除或改道。

### 1.5 jobs.yaml 加 `line` 字段

**文件**：`utils/job_loader.py` + `inputs/jobs.example.yaml`

**实现**：每个 job 增加 `line: aav|solidex`；job_loader 校验 line 存在；PDF 路径默认在 `inputs/pdfs/{line}/` 下找。

**验收**：jobs.yaml 一条 job 指定 line 后能正确路由到对应线配置 + 提示词。

---

# Phase 2：数据模型解耦 + 流程分两段

## 目标

把"生成即发布（1:1）"拆成"生成基准正文（内容层）→ 扇出多平台（投放层）"，DB 从 1:1 扩成 1:N。

## 任务清单

### 2.1 DB 重塑：line / article / distribution

**文件**：`db/database.py`

**新结构**：
```
lines(line_id, name, ...)                       -- 线登记（也可只走配置文件，DB 存引用）
articles(id, line_id, job_id, pdf_path,         -- 基准正文（平台无关）
         title, digest, content_dir, model,
         tokens..., status, created_at)
translations(id, article_id, lang, content_dir, -- Phase 5 用，先建表预留
             status)
distributions(id, article_id, platform,         -- 1:N：每个落地实例
              account, lang, module_id,
              assembled_dir,                     -- 组装成品落盘路径
              publish_status, wechat_media_id,
              wechat_url, external_url,
              publish_error, created_at)
```

**注意**：沿用落盘/库分离 —— `content_dir` / `assembled_dir` 存路径，DB 不塞大文本。

**验收**：迁移脚本能在已有 Phase 0 库上平滑升级（ALTER + 新表）；1 篇 article 能挂多条 distribution。

### 2.2 batch_processor 拆 generate 阶段

**文件**：`batch_processor.py`

**实现**：`generate` 子命令：load jobs → 逐 job 出基准正文 → 落盘 `outputs/articles/{article_id}/` + 写 `articles` 表（status=generated）。**不碰任何平台**。

**验收**：`python batch_processor.py generate` 跑完，DB 有 article 行、outputs 有产物，零网络发布。

### 2.3 distribute 阶段骨架（先只接公众号）

**文件**：`batch_processor.py` + 复用现有 `wechat_client`

**实现**：`distribute` 子命令：读 generated article → 为指定平台建 distribution 行 → （Phase 3 接组装）→ 发草稿。先把单公众号链路在新结构上跑通（沿用 Phase 0 的 create/update_draft + PATCH 逻辑，但写到 distributions 表）。

**验收**：`generate` 后 `distribute --platform wechat` 能把一篇 article 发到公众号草稿；重跑走 PATCH 不新建。

### 2.4 向后兼容验证

**验收**：新两阶段流程跑出来的草稿，跟 Phase 0 单线单公众号结果一致（不回归）。

---

# Phase 3：产品模块组装（line × platform）

## 目标

把"基准正文"在投放前拼上"该 line × 该 platform 的固定产品视觉模块"。

> 模块**具体字段 / 资产格式**用户已说"后面设计"。本阶段先定**粒度（line×platform）+ 注入时机（组装阶段）+ 可配置化**，schema 边做边定。

## 任务清单

### 3.1 产品模块配置系统

**文件**：新建 `inputs/product_modules/{line}-{platform}.yaml` + `utils/product_module_loader.py`

**配置（初版，可扩）**：
```yaml
module_id: aav-wechat
line: aav
platform: wechat
series_image: assets/aav_series.png    # 系列图（本地路径，组装时上传）
qrcode: assets/aav_qr.png              # 二维码（公众号用）
external_url: ""                       # 外链（blog 用；公众号会被吞）
cta_text: ""                           # 可选补充文案（注意软广文字主体已在正文，见 PROJECT 5.1）
```

**验收**：能按 (line, platform) 取到模块；缺模块时明确报错或留占位。

### 3.2 组装器

**文件**：新建 `utils/assembler.py`

**实现**：`assemble(article_html, module, platform) -> str`：基准正文尾部拼接平台模块（图/链/码），输出平台成品 HTML/blob，落盘 `outputs/distributions/{...}/`。**不同平台模块不得硬编码**，全读配置。

**验收**：`aav-公众号` 拼系列图(+二维码)、`aav-blog` 拼系列图+外链，两者成品不同且都正确落盘。

### 3.3 模块资产上传与缓存

**文件**：`utils/wechat_client.py`（uploadimg 已有）+ manifest

**实现**：模块里的系列图/二维码上传公众号拿 mmbiz URL；用 `runtime/image_upload_manifest.json` 缓存避免重复上传（公众号 5000/日配额）；同图命中缓存只传 1 次。

**验收**：同一系列图组装 5 次，实际只调微信 API 1 次（mock 验证 call_count）。

---

# Phase 4：多平台投放 adapter

## 目标

把投放抽象成可插拔 adapter；先打通公众号多账户，blog/领英留可插拔占位。

## 任务清单

### 4.1 投放 adapter 抽象

**文件**：新建 `utils/publishers/base.py`

```python
class Publisher(Protocol):
    def publish(self, distribution, assembled_content) -> PublishResult:
        """发布/更新一个 distribution，返回 media_id/url + 状态"""
```

**验收**：接口定义清晰；distribute 阶段通过该接口调具体平台。

### 4.2 公众号多账户（token 隔离 — PROJECT 10.1 头号坑）

**文件**：`utils/wechat_client.py` + `utils/publishers/wechat.py`

**实现**：
- `WeChatClient` 以 app_id 为缓存键，token 文件 `runtime/wechat_token_{account}.json` 隔离
- 配置 AAV 账户 + 免疫客账户两套 `WECHAT_{ACCOUNT}_APP_ID/SECRET`
- `wechat.py` 实现 `Publisher`：create/update draft + PATCH 重发

**验收**：同一 article 扇出到两个公众号账户，两 token 不互踩；各自草稿独立、重发走 PATCH。

### 4.3 genemedi blog adapter（待网站接口）

**文件**：新建 `utils/publishers/blog.py`

**实现**：先实现到"组装成品落盘 + 调用占位接口"；等网站人员给 post 脚本接口后接真实端点。`{lang}` 维度此处体现（配合 Phase 5）。

**验收**：blog adapter 接口就位，成品落盘；真实接口 TODO 标清。

### 4.4 LinkedIn adapter（占位，待调研）

**文件**：新建 `utils/publishers/linkedin.py`（骨架）

**验收**：占位 adapter 存在，不阻塞其它平台；调研结论记到本文件。

---

# Phase 5：AI 翻译阶段（移植 target-running）

## 目标

genemedi 多语言：基准正文生成后、组装前，跑独立 AI 翻译。直接复用 target-running 那套。

## 任务清单

### 5.1 移植翻译模块

**文件**：新建 `utils/translator.py`（移植自 target-running）

**实现**：`translate(article_markdown, target_lang) -> markdown`；接在 generate 之后、组装之前；只对需要多语言的平台（genemedi）触发。

**参考**：target-running 的翻译实现（`D:\dev-project\target-running\.claude\worktrees\epic-sanderson\`）。

**验收**：中文基准正文 → `{lang}` 译稿，落盘 `outputs/translations/{...}/` + 写 `translations` 表；质量对齐 target-running。

### 5.2 翻译接入 distribution

**实现**：genemedi distribution 携带 `lang`，组装时取对应译稿而非基准正文。

**验收**：同一 article 的 `en` / `zh` blog distribution 取到各自语言成品。

---

# Phase 6：质量安全网

## 目标

杜绝损坏稿 / 营销腔 / **正文夹带产品**（新架构下正文本应纯科普）流到投放。

## 任务清单

### 6.1 markdown 健康度安全网（移植）

**文件**：新建 `utils/health_check.py`（移植自 target-running）

**实现**：`_markdown_health_score(md) -> int`；拒绝条件：以 `<` 开头 / 含 `===PART_` / 长度过短 / 没 H1 / H2 过少。

**源码参考**：`D:\dev-project\target-running\.claude\worktrees\epic-sanderson\core\main.py`

### 6.2 科普调性自审 + 正文夹带产品扫描

**文件**：新建 `utils/tonal_qa.py` + `prompts/tonal_qa.system.md`

**实现**：
- 静态扫描：硬广词黑名单（`data/hard_ad_words.txt`）+ **正文出现产品名 = 违规**（产品只该在结尾一句 + 模块里）
- LLM 评分：科普中立度 0-100 + 改写建议
- `score < THRESHOLD` 或正文夹带产品 → `publish_blocked`

**验收**：故意让正文中段插产品被拦；正常纯科普稿不误杀；被拦稿仍落盘供人工 review。

### 6.3（可选）多模型并行 + merger

**文件**：`core/main.py` + `utils/verifier.py` + `prompts/healer.system.md` / `merger.system.md`

**实现**：env 开关 `MULTI_ROUTE_ENABLED`（默认关）；3 路并行 → reviewer → merger；移植 target-running 并去掉 UniProt/fact_finder。候选稿落盘，DB 只存路径。

**验收**：开关 on 质量提升、off 行为不变（向后兼容）。

---

# Phase 7：Dashboard + 人工抽检 + 重生成/重投放

## 目标

浏览器看「内容 × 投放」矩阵；人工抽检闸；重生成正文 / 重投放单平台走 PATCH。

## 任务清单

### 7.1 Flask app + 矩阵视图

**文件**：`app.py`（改造自 target-running）+ `templates/` + `static/`

**端点**：`/api/workflow/matrix` 返回 article × platform 网格（每格 distribution 状态）。

### 7.2 单篇预览

**端点**：`/preview/{article_id}`（基准正文）+ `/preview/{distribution_id}`（平台成品）。

### 7.3 人工抽检闸

**端点**：`/api/workflow/gate` + `workflow_settings` 表。生成→投放之间挂"待人工抽检"，闸开时需手动放行。

### 7.4 重生成 / 重投放级联

**实现**：
- 重生成正文：`cascade_reset` article + 其下所有 distribution 置 pending（**保留 media_id/url** 给 PATCH）
- 重投放单格：只重发该 distribution，走对应 adapter 的 PATCH（公众号 draft/update、blog 的 update 端点）
- **URL/位置稳定**（沿用 target-running 设计），旧外链/SEO 不丢

### 7.5 余额 / 计费可视化

**端点**：`/api/accounts/overview`（复用 `utils/account_providers.py` + `utils/moonshot_billing.py`）。

---

# 各阶段共通：测试

| Phase | 必须的测试 |
|---|---|
| 1 | `test_line_loader.py`、`test_main.py`（双线 prompt 组装 + 方案 B 注入） |
| 2 | `test_database.py`（1:N 迁移）、`test_batch_processor.py`（两阶段） |
| 3 | `test_product_module_loader.py`、`test_assembler.py`（line×platform 组装）、上传缓存 call_count |
| 4 | `test_wechat_client.py`（多账户 token 隔离）、`test_publishers.py` |
| 5 | `test_translator.py`（mock LLM） |
| 6 | `test_health_check.py`、`test_tonal_qa.py`（含正文夹带产品扫描） |
| 7 | `test_dashboard_api.py`、`test_cascade_reset.py` |

---

# 不在范围内（但可能将来要做）

- 公众号自动发布（`freepublish`）—— **永远不要**，必须人工
- 内容分发到知乎/头条等其它平台 —— 另起项目，别塞进来
- AI 出封面图/插图（fal-ai / dreamina）—— 可作为图片来源的可选实现
- OCR 扫描版 PDF —— 加 `utils/ocr_extractor.py`，pdf_extractor 失败时回退
- PDF 网页源自动抓取 —— 加 `utils/{line}_fetcher.py`，替换"人工上传"入口
- 文章数据看板（PV/UV/分享）—— 非优先级

---

# 给接手者的建议工作流

1. **第一次接手**：读 `README.md` → `docs/PROJECT.md` → 本文件
2. **挑任务**：Phase 1 → 2 是硬前置，先做；之后 3/4/5/6 看人手并行
3. **写之前**：grep 相关已有逻辑（尤其 `utils/` 和 `core/`）
4. **写完后**：跑 `python -m pytest tests/ -q` + 加针对性单测 + 真实小样例 dry-run
5. **不确定的设计**：参考 target-running 同名/类似模块（路径见 PROJECT.md 第 2 节），但**绝不改 target-running**
6. **commit 风格**：`feat: xxx` / `fix: xxx`，首行简短、正文中文说清动机和影响
