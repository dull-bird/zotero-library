# 原论文、出版版本、译文和归档修订

## 不混淆的几种版本

| 概念 | 例子 | 如何确定 |
|---|---|---|
| 文献身份（work） | `arxiv:2509.19297` | 稳定 arXiv ID；不同 vN 不重复建论文 |
| 书目所选版本／edition | arXiv v2、核验后的会议／期刊记录 | 官方记录，不能混用不同版本的日期和卷期页码 |
| 原文版本 | arXiv v1/v2/v3、accepted manuscript、publisher 声明的 revision | 每个 PDF 单独记录平台、标识符、版本与依据；未知就留未知 |
| 译文自身修订 | 翻译任务声明的 t1/t2 | `translation_revision`，独立于原文版本；工具不猜翻译者的修订号 |
| 本工具归档修订 | r1/r2 | 同文献、同附件角色出现新 SHA-256 时自动编号；只是本地归档序号，不是官方论文版本 |
| Zotero 对象版本 | `clientVersion` | 仅用于写入并发控制，绝不能当作论文 vN |

示例：

```text
一条论文记录
├── 原文 · arXiv v1 · 归档 r1
├── 中文译文 · arXiv v1 · 译文 t1 · 归档 r1
├── 原文 · arXiv v2 · 归档 r2
├── 中文译文 · arXiv v2 · 译文 t1 · 归档 r2
└── 中文译文 · arXiv v2 · 译文 t2 · 归档 r3
```

一份译文可升级翻译而原文仍是 v2。元数据查询返回 v3，也不能把上述 v1/v2 附件重新标为 v3。

## 默认自动做的事

- 归档按 SHA-256 识别内容；相同字节复用，不同字节作为新附件保留，旧 PDF 和标注不覆盖。
- 每个附件保存 `source_edition`（平台、文献标识、源版本、URL、证据），可有 `derived_from_sha256` 指向原文。
- 新内容自动分配本地归档 rN，重导入相同内容沿用旧 rN。
- 根据本工具的结构化来源笔记恢复版本历史；普通用户笔记不解析为版本数据。
- 无 arXiv 版本号的旧译文保持“原文版本未知”。文件名含 `_v2` 只是线索，不自动认定。
- 外部 sidecar／第三方工具自称“下载已验证”不直接相信；仅在本机私有下载记录匹配时保留下载验证标志。

同一字节可能真的对应两个上游版本（例如只修改元数据）。初版遇到既有明确版本和新版本冲突时，保留文件并要求复核来源，而不是自动重贴标签；不宣称这种情况不可能。

## arXiv：明确版本与固定下载

`manifest --arxiv 2509.19297v2` 会保留 v2，解析时按 v2 查询；若 API 未返回 v2，不偷偷退回 latest。书目元数据版本与每个附件声明的版本仍分开。

要有可追溯原文下载，用明确 vN：

```bash
python3 <skill-dir>/scripts/zotero_library.py fetch-original \
  --arxiv 2509.19297v2 \
  --destination /absolute/papers/2509.19297_v2_EN.pdf
```

工具先核验该版本元数据，再下载固定版本 PDF，记录 URL、哈希、大小、时间和官方元数据哈希。返回一个私有 receipt 路径，另在 PDF 旁保存 `.source.json` 便于迁移。不会覆盖已有目标文件。

翻译完成后：

```bash
python3 <skill-dir>/scripts/zotero_library.py manifest \
  --arxiv 2509.19297v2 --title '<official full title>' \
  --source-root /absolute/papers \
  --original /absolute/papers/2509.19297_v2_EN.pdf \
  --translation /absolute/papers/2509.19297_v2_CN.pdf \
  --translation-revision t1 \
  --receipt /private/download-receipts/<sha256>.json \
  --out /private/manifest.json
```

receipt 会校验原文哈希及 work/version，并记录译文配对的原文哈希。它不证明译文准确、完整；视觉／内容核验仍由翻译流程承担。

## 检查新版，不自动替换

在文献整理任务中，agent 可运行：

```bash
python3 <skill-dir>/scripts/zotero_library.py versions \
  --catalog /private/catalog.json --out /private/versions.json
```

默认复用日级元数据缓存；用户明确问“现在是否有新版”时用 `--fresh`。不要把提供的旧 XML 当实时查询：`--fresh` 与 `--xml` 互斥。

报告区分：
- 最新已知 arXiv 版本；
- 所选书目版本；
- 附件／历史记录中已声明的原文版本；
- 未知版本附件数；
- 是否比**已知**附件版本更新。

全部附件版本未知时，不会把“未发现可比较的旧版本”说成“全部已是最新版”。检查操作不下载新 PDF、不重跑翻译、不改数据库。用户要求获取／翻译新版时才生成固定版本下载和新附件。

没有常驻 watcher：自动操作发生在 agent 处理相关任务时，而不是在退出会话后持续监控 arXiv。

## 其他平台

- Crossref 出版记录保留平台声明的 version（若有）、DOI 以及 `relation`（如 preprint/version 关系）；检查时可报告书目信息变化。
- DOI 本身不保证存在 v1/v2 计数；`deposited`／`indexed`／更新时间改变不等于论文正文升级。
- 接收稿、正式出版版、修正版、会议版、期刊扩展版要使用平台明确标签；可能是不同 DOI，也可能不是同一篇作品。
- 每个平台的版本字段需要独立适配。初版没有自动抓取 OpenReview、所有出版网站的完整版本历史；可保存已核验的来源标签，不能假装已有通用版本探测。
- 无明确版本号时，仍可依靠本地归档 rN 和 PDF 哈希保留变化，但显示“上游版本未知”，不能擅自生成一个官方 v2。
