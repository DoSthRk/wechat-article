# Prompt 变更日志

本项目 prompt 的人读变更记录。版本控制依赖 **git 历史**；本文件记录每次"改了什么、为什么"，
便于回溯与 A/B 质量对比。每条尽量附上对应 git commit（短 hash）。

## Prompt 文件清单

- `prompts/base.system.md` —— 共通基底（方案 B 契约：正文零产品、配图来自 PDF、结尾一句点名固定产品）
- `prompts/lines/aav.md` —— AAV 工艺智库线写作侧重
- `prompts/lines/solidex.md` —— Solidex 肿瘤免疫线写作侧重

> 注：各 line 绑定的**风格模板**（`inputs/style_templates/*.yaml`）和**产品**（`inputs/products/*.yaml`）
> 是用户数据、被 gitignore，不在此版本管理范围内；它们的字数 / 禁用词等约束按篇维护。

---

## 2026-06-04 · base.system.md 调性微调（两条线首次真跑后）

**改了什么**
- 核心原则补第 6 条：正文与**小标题**禁用"降维打击 / 碾压 / 吊打 / 秒杀 / 神器"等口语化爽词。
- 输出契约补"字数硬约束"：正文必须落在模板字数区间内，接近上限主动精简，绝不超上限。

**为什么**
首次真跑（Glofitamab 免疫客 / AAV spatial genomics）后的观察：
- Glofitamab 实样 ~2200 字，超 oncology 模板 1800 上限 → 收紧字数。
- Glofitamab 实样把"降维打击"用作了医学小标题，偏营销腔 → 禁用爽词。

方案 B 主体（正文零产品 + 结尾自然点名 + 配图来自 PDF）已验证通过，本次仅收紧调性与篇幅，未动结构。

---

## 2026-06-04 · 初始（P1' 方案 B 双线）

- 新建 `base.system.md`：确立方案 B 契约（正文纯科普、产品只在结尾一段出现一次）。
- 新建 `lines/aav.md`、`lines/solidex.md`：两条线写作侧重。
- 对应 commit：`5ea5ec3`（AAV 线 + line 一等概念）、`e78e483`（Solidex 线）。
