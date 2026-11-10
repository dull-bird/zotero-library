# 源码核验与架构选择

## 不把 MCP 的宣传当成 Zotero 的行为

参考过：
- `54yyyu/zotero-mcp`：MCP／CLI 双入口、本地 SQLite 快照读取、Zotero 10 本地写入授权。
- `cookjohn/zotero-mcp`：Zotero 内置插件／Streamable HTTP 方案；下载核验的 1.6.0 manifest 声明兼容到 10.99.99，但没有安装插件。兼容声明不等于已实测。
- `shoei05/claude-code-zotero-skill`：Connector 导入的轻量思路。
- `WenyuChiou/zotero-skills`：本地读、云端写的方案；本工具不要求同步，不采用其云端写入依赖。

核心 Python 代码为独立实现，没有随仓库分发上述项目的源码，不依赖这些包，也不自动运行第三方安装脚本。第三方 MCP 可由用户单独配置给其他客户端；本仓库不实现或启动独立 MCP Server。

MCP 是工具调用协议，不是权威元数据库，也不会自动让字段更准确。对于有 shell 的 agent，独立 CLI 可把重复工作合并成一次计划和小批量请求，不需要在每轮对话里发送几十个工具 schema。具体性能仍应测量，不声称这是所有场景的“最快实现”。

## 核验来源

主要依据本机 `/Applications/Zotero.app/Contents/Resources/app/omni.ja` 内的源码及 schema，核验版本为 **10.0.8、schema 44**。

查看过 upstream `zotero/zotero` main，当时 commit 为 `52048d4e7ad54a4c1986af9cc8b0c1f331a02442`。不假定它与已安装版本逐字相同；运行中的安装版本是实际行为依据。

主要文件：

| 文件 | 核验内容 |
|---|---|
| `chrome/content/zotero/xpcom/server/server_localAPI.js` | 本地读写授权、server ID、版本检查、批量响应、附件三阶段上传 |
| `chrome/content/zotero/xpcom/server/server_connector.js` | Connector 导入和二进制附件；只用于理解已有失败，不作权限绕过 |
| `resource/schema/global/schema.json` | `preprint`、`report`、`journalArticle` 等真实字段／作者类型 |
| `chrome/content/zotero/xpcom/utilities/date.js` | `multipartToStr`：数据库日期可能是 `2026-00-00 2026`，API 日期不是这种存储串 |
| `chrome/content/zotero/xpcom/data/item.js` | `getLocalFileURL` 和附件路径／缺文件行为 |

`source-audit` 从当前安装包重新提取 SHA-256 和实际 schema，不把上述旧版本号当作永恒保证。计划保存哈希，安装包／schema 在计划后变化则拒绝执行，需重新审阅与测试。

```bash
python3 <skill-dir>/scripts/zotero_library.py source-audit --out-dir /private/audit
```

## 已核验的接口契约

1. `/api/` 和其他本地 API 受 `httpServer.localAPI.enabled` 控制；默认可返回 403。
2. 读请求不需要写密钥。写请求要求本地 API key，并附 `Zotero-Server-ID`。
3. `POST /api/local/authorize` 由用户弹窗授权。一次允许的 key 只能使用一次；持久授权必须由用户选择。
4. `POST /api/users/0/items`、`collections` 支持对象数组。HTTP 200 仍可能有 `failed` 条目，必须逐项检查。
5. 有 key 的写入需要 `version` 等并发前提；对已存在对象比对的是 **clientVersion**，不是云端同步 version。
6. 本工具在写前生成／保存稳定 key，显式使用 `version=0` 创建对象，避免响应丢失后无依据地重复创建。
7. 文件上传分为授权、上传原始二进制、登记。声明 MD5、文件名、大小、毫秒 mtime；新文件使用 `If-None-Match: *`；登记后再校验 SHA-256。
8. 上传 receiver 的一次性 upload URL 本身是能力凭证。只接受同一个 loopback origin 和预期路径，不转发到其他主机、不跟随重定向。
9. 附件 URL 不保证本地文件已经存在；空文件 URL／缺文件是可恢复的部分导入状态，不假装已完成。
10. SQLite 读取要包含 WAL；不能简单以主数据库文件内容为空来断言文献库为空。

## 效率与安全取舍

- 读取：稳定 DB+WAL 快照，一次批量读取；不让 SQL 接触源数据库的写接口。
- 写入：每批最多 50 个元数据对象；文件流式上传，避免一次性把全部 PDF 放进内存。
- 来源：arXiv 元数据按 ID 批量请求，缺失才联网，有缓存和请求间隔；公共 API 只接收公开标识符，不接收 PDF 内容。
- 版本：固定源 vN 与归档 rN 分离，文件哈希识别新内容；不把元数据更新、数据库 clientVersion 当论文修订。未知源版本不猜。
- 常驻资源：核心工具按需运行，无 daemon、无向量模型、无额外端口。
- 并发：本工具进程锁，加 server ID 与对象版本；外部编辑后失败即重新计划。
- 恢复：先备份，后写意图日志；超时不盲重试，按稳定 key 和实际字节重新核验。

## 测试诚实性

测试覆盖身份冲突、字段保护、multipart 日期、WAL 快照、上传流／三阶段契约、并发版本冲突、重复执行、部分上传恢复、现有 PDF 不覆盖、跨主机上传拒绝，以及 arXiv 固定版本、原文与元数据版本分离、自动归档 rN、旧附件保留、更新检查不修改文件、外部下载 receipt 不盲信等。

已在上述安装版本完成真实单篇、两篇批量和 17 篇全量原生写入验收；重复全量计划没有发出写入请求，17 个存储 PDF 的哈希和 PDFKit 可读性均已核对。详情见 [TESTING.md](../TESTING.md)，原始日志和库内容不公开。

这些结果不等于其他平台／版本都已验收，也不等于真实测试了所有失败分支。**不能把离线 mock 通过说成已经对目标用户的真实 Zotero 完成写入验收。** 新安装第一次执行仍应先用一篇核验，再扩到全部。
