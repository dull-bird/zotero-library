# 操作、文件与恢复

## 依赖与范围

- Python 3.9+，仅标准库；macOS 本机 Zotero 10+。
- 默认应用 `/Applications/Zotero.app`，数据库 `~/Zotero/zotero.sqlite`。
- 自定义数据目录使用 **全局参数在子命令前**：`python3 TOOL --db /path/zotero.sqlite doctor`。
- 核心不依赖 `zotero-cli`、MCP、Zotero 账号、第三方插件、浏览器自动化、辅助功能权限或模型密钥。
- 自动解析覆盖 arXiv；Crossref 出版版本作为单独候选严格核验。不是 ISBN、所有 DOI 注册机构或任意格式 PDF 的万能识别器。其他类型的数据模型／分类受 Zotero schema 支持，但引入新来源需独立适配和测试。

## 命令

| 命令 | 作用 | 改 Zotero？ |
|---|---|---|
| `translation show / set / reset / route` | 用户级翻译方式（当前 agent 或指定 CLI）；未选先问，路由不调用模型 | 否 |
| `doctor` | 当前安装源码、schema、本地 API、真实库计数 | 否 |
| `source-audit --out-dir DIR` | 保存源码哈希及 schema | 否 |
| `inventory --out FILE` | 私有 DB+WAL 快照读出的条目与分类 | 否 |
| `scan --repo DIR --out FILE` | 读取索引，核验路径及 PDF 哈希，生成 manifest | 否 |
| `manifest ... --out FILE` | 一个文献、多 PDF 的 DeepOrbit 入库清单，保留明确 vN／译文修订／receipt | 否 |
| `fetch-original --arxiv IDvN --destination FILE` | 下载一个固定版本原文，生成哈希 receipt，不覆盖旧文件 | 否 |
| `versions --catalog FILE --out FILE` | 查询最新已知源版本，区分未知附件；`--fresh` 绕过日缓存 | 否 |
| `resolve --manifest FILE --out FILE` | 官方 arXiv 元数据与来源记录 | 否 |
| `doi DOI --out FILE` | Crossref 出版版本候选 | 否 |
| `select-edition ...` | 核验并选择出版版本，输出新 catalog | 否 |
| `plan --catalog FILE --out FILE` | 查重、补字段、分类、冲突计划 | 否 |
| `authorize` | 请求用户本地写入授权 | 只保存用户选择的授权 |
| `apply --plan FILE` | 原生 API 写入并校验 | 是 |
| `verify --plan FILE` | 对照计划检查实际字段、分类、标签、附件归属／哈希 | 否 |
| `sync --repo DIR`／`sync --manifest FILE` | 串起 scan／resolve／plan | 否 |
| `sync ... --apply` | 上述流程加 apply／verify | 是 |

完整参数见 `python3 TOOL --help` 和每个子命令的 `--help`。

正常输出是一份 JSON。注意区分顶层命令成功 `ok`、verification 的 `ok`、单篇 `write_verified` 和 `metadata_complete_for_selected_edition`。解析失败或验证失败会给非零退出码；发现 API_DISABLED 不能当作库为空。

## 私有运行数据

默认根目录：`~/.local/share/zotero-library/`。

```text
authorization.json       # 仅用户选择持久授权后保存，0600，不能读给模型／发到聊天
config.json              # 私有翻译偏好，0600；不自动读取项目提供的命令配置
apply.lock               # 阻止本工具同时执行两次 apply
metadata-cache/          # 官方公共元数据缓存
download-receipts/       # 固定版本原文下载凭证；外部伪造的同名 sidecar 不自动可信
version-checks/          # 显式实时版本查询的原始证据缓存
preparations/<run>/
  manifest.json
  catalog.json
  plan.json
runs/<run>/
  database-backup/        # 写前 DB+WAL 快照；不能直接拿来覆盖运行中的库
  plan.json              # 精确变更范围
  journal.json           # 请求前意图、完成／不确定状态、时间戳，不含写密钥
  verification.json      # 所选字段与实际 PDF 的校验结果
```

本地 manifest／计划可能包含私有仓库地址和绝对文件路径，默认不要上传或提交 Git。Zotero 中生成的来源笔记不包含绝对缓存路径；只记录来源 URL、相对路径、哈希、日期、版本不确定性等。

用户明确要求备份／分享时，排除 authorization.json。源数据库不可放进 Google Drive 实时同步目录；离线备份可以上传。

## 异常恢复

- `API_DISABLED`：由用户在 Zotero 设置中启用本地通信，不代改偏好设置。
- `AUTH_REQUIRED`：提示用户授权，停止；不转云端、不直接写 SQLite。
- `STALE_VERSION`／`SERVER_CHANGED`：停止并重读文献库，重建计划；不重用旧 version 硬写。
- `REQUEST_UNCERTAIN`／`PARTIAL_FAILURE`：先读实际对象。稳定 key 已保存，重新计划／执行应补缺项，不生成新 key 掩盖问题。
- `SOURCE_CHANGED`：重新扫描 PDF 或审阅升级后的安装源码／schema。
- `FILE_CONFLICT`：保留原附件；不得把源 PDF 覆盖上去。
- 身份／标题／作者不符：放待复核，不能用“字段看起来更完整”作为替换理由。
- `VERSION_CONFLICT`：同一字节有矛盾的源版本记录时复核，不把旧 v1 自动重贴成 v2；保留两次来源证据。
- 版本号未知：不是故障，也不是“已是最新版”；不凭 filename／最新元数据补猜。
- `TRANSLATION_CHOICE_REQUIRED`：先问用户用当前 agent 还是指定 CLI，保存明确选择后再开始新翻译。
- `TRANSLATION_CLI_NOT_FOUND`／外部 CLI 认证失败：停止并说明，不擅自换模型。路由解析成功也不代表模型实际调用成功。

不会自动删除写了一半的条目或恢复整库，因为这可能破坏用户在此期间新增的内容。要回退先读运行日志，列出确切新增对象，征求确认后通过 Zotero 支持的接口处理。完整备份恢复必须退出 Zotero，且应另行指导，不能趁程序运行时替换数据库。

## 首次真实写入验收

1. 开启原生 API，由用户确认写入授权。
2. 从完整计划中挑一篇（优先已有条目缺附件的文献）生成独立的小计划，确保只补齐该条。
3. 执行并核对真实 API 返回、字段、附件 parent、SHA-256 和可打开性。
4. 重新读取库并为剩余论文生成计划，不重复执行旧的无 key 导入。
5. 总结新增／复用／复核数量，明确仍缺的官方字段。第一次真实写入若不符合源码预期，修工具并加回归测试，不把其余文献当作试验品。

版本历史保存在结构化来源笔记中，原文／译文的 rN 按角色分别计数，和 Zotero 对象 version 完全不同。详见 [versions.md](versions.md)。

## 让 skill 生效

该目录位于 `~/.agents/skills/zotero-library`，Pi 原生会发现标准 Agent Skills 目录。当前会话中编辑后运行 `/reload`，可显式调用 `/skill:zotero-library`；新会话按描述自动匹配。这里的“默认操作”指 agent 收到相关任务时自动使用，不是无需会话的后台同步。
