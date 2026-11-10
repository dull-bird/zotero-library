# Zotero Library Curator

本地优先的 **Zotero 文献整理 CLI + Agent Skill**：官方元数据核验、一篇多 PDF、多维分类、原文／译文版本追踪、可审计的原生 API 写入。

A local-first Zotero curation CLI and Agent Skill. One bibliographic record per work, multiple version-aware attachments, source-verified metadata, additive changes and verifiable native writes. No cloud account, mandatory MCP server or Zotero plugin.

> **状态：可使用的早期版本，不是 Zotero 官方项目。** 已在 macOS arm64、Zotero **10.0.8 / schema 44** 上完成真实单篇、小批量、17 篇全量和重复执行验收。其他 Zotero 版本与平台没有同等真实验收；首次使用请先做小计划。详见 [TESTING.md](TESTING.md)。

## 做什么

- **核验而不是猜字段**：arXiv 官方元数据；Crossref 出版版本需检查标题、作者和 DOI。来源没有的卷期、页码、许可证等留空。
- **一篇论文一条记录**：英文原文、中文译文、补充材料作为附件，不因译文或 arXiv vN 重复建论文。
- **版本分层**：原文 vN、译者声明的修订、本地归档 rN 分开；新文件保留为新附件，不覆盖旧 PDF 或标注。
- **未知就未知**：最新元数据不能证明旧译文来自哪个版本；DOI 更新时间不是正文版本号。
- **多维分类**：顶层“文献库”汇总全部受管理论文，再按主题、类型、年份和来源分类，不复制 PDF。
- **先计划再执行**：去重、冲突保护、稳定对象 key、server ID、对象版本与来源哈希检查。
- **写后验证**：逐项检查批量响应、附件 parent 和实际存储文件 SHA-256，不把 HTTP 200 当作全成功。
- **可恢复**：写前保存 DB+WAL 备份，记录写入意图和验收结果；不自动删除或恢复整库。

核心是 Python 标准库 CLI，不是 MCP Server；shell-capable agent 通过 skill 调用它即可。没有后台 watcher、向量模型、AI API 客户端或第三方 Python 运行依赖。

## 安装

需要 Python **3.9+**，建议使用仍获安全维护的 Python 版本；3.9 是兼容下限。已验证的原生写入环境为 macOS 上的 Zotero **10.0.8**。

```bash
git clone https://github.com/dull-bird/zotero-library.git
cd zotero-library
python3 scripts/zotero_library.py --help
python3 scripts/zotero_library.py doctor
```

将仓库作为 Agent Skill 使用：

```bash
mkdir -p ~/.agents/skills
ln -s /absolute/path/to/zotero-library ~/.agents/skills/zotero-library
```

如果目标路径已有内容，不要强行覆盖；先检查并备份。Pi 中执行 `/reload`，再用 `/skill:zotero-library`，或者直接请求整理文献。其他支持标准 Agent Skills 的客户端可以读取根目录 [SKILL.md](SKILL.md)。

## 第一次写入

1. 保持 Zotero 运行，在 **设置 → 高级** 开启 *Allow other applications on this computer to communicate with Zotero*。
2. 先检查安装源码、schema 和文献库：`doctor`。
3. 发起授权，**由用户亲自选择权限**：

   ```bash
   python3 scripts/zotero_library.py authorize
   ```

持久授权适合多请求批处理，但必须由用户决定。一次性授权不会持久化，当前 CLI 的分步批量流程需要可重复使用的授权。禁止代点确认、代改偏好设置或绕过权限。

## 整理一个本地译文仓库

支持四列、无表头的 `index.tsv`：`arXiv ID`、原文标题提示、翻译日期、相对 PDF 路径；缺失 ID 应通过 PDF 完整标题和作者核验，不按短文件名猜测。

```bash
# 仅生成计划，不写入 Zotero
python3 scripts/zotero_library.py sync \
  --repo /absolute/path/to/paper-repository \
  --out-dir ~/.local/share/zotero-library/preparation

# 审阅计划及冲突后执行；第一次只用一篇的小计划
python3 scripts/zotero_library.py apply \
  --plan ~/.local/share/zotero-library/preparation/plan.json
```

也可用 `manifest` 为一篇论文声明原文、译文和补充附件。支持 `fetch-original --arxiv IDvN` 的固定版本下载、下载 receipt 与只读 `versions` 更新检查。新翻译任务请固定明确 vN，而不是在任务不同阶段重复使用 latest。

所有命令：

```text
doctor · source-audit · inventory · scan · manifest · resolve · doi
select-edition · plan · authorize · apply · verify · sync
fetch-original · versions
```

详细流程见 [operations.md](references/operations.md)、[metadata-policy.md](references/metadata-policy.md)、[versions.md](references/versions.md) 和 [DeepOrbit 集成说明](references/deeporbit-integration.md)。

## 安全边界与限制

- **不直接 SQL 写 Zotero 数据库**。数据库读取使用稳定 DB+WAL 副本；写入只走授权的原生本地 API。
- 不自动删除、合并、覆盖非空重要字段、丢弃旧附件、开启同步或上传文献内容到云服务。
- 授权密钥、数据库、PDF、计划、缓存、receipt、备份和日志是私人运行数据；**不要提交到这个仓库**。工具不启用云同步，但 Zotero 已有的同步／存储设置仍然生效。
- arXiv 与 Crossref 请求使用公开标识符；不向它们上传 PDF。来源检查仍会进行公共网络请求。
- 初版主要覆盖 arXiv 论文及翻译归档，不是所有书籍／扫描 PDF 的自动识别工具，也没有通用的出版网站版本历史爬虫。
- 主题规则主要面向计算机视觉、规划和医疗 AI，可审阅和扩展，不等同于人工语义判断。
- 完成所选版本的已核验字段，不表示所有字段都能填满；译文质量和来源版本仍可能未知。

更多安全说明见 [SECURITY.md](SECURITY.md)。

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts
```

离线测试不连接真实 Zotero，也不需要安装 Zotero。真实写入验收范围和未覆盖部分见 [TESTING.md](TESTING.md)。

```text
SKILL.md                     # agent 入口、权限边界和工作流
scripts/zotero_library.py    # CLI
scripts/zotero_core.py       # 原生 API、源码/schema、只读快照
scripts/zotero_catalog.py    # arXiv/Crossref、身份核验、分类
scripts/zotero_workflow.py   # plan/apply/verify、日志与来源笔记
scripts/zotero_versions.py   # 源版本、归档修订、固定下载、更新检查
references/                 # 策略和操作文档
tests/                      # 单元、mock 工作流和 loopback HTTP 契约测试
```

## 许可证

尚未指定开源许可证。公开可见不等于已经授予任意再分发或再许可权；如需复用，请先与维护者确认。
