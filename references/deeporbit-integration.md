# DeepOrbit arXiv 翻译 → Zotero 入库

这是 `do.arxiv-translator` 风格工作流的可选后处理接入说明，不依赖某个用户的私有仓库或配置。接入前先阅读目标仓库当前的 `AGENTS.md` 和 `skills/do.arxiv-translator/SKILL.md`，按其实际约定调整。

本 skill 不擅自修改或推送另一个仓库，也没有安装后台 watcher。

## 翻译开始前的用户选择

先按 [translation.md](translation.md) 读取本机翻译偏好：未配置时询问使用当前 agent 还是指定 CLI，用户明确选择后再保存。CLI 失败不得默默回退到别的模型；这项选择不改变聊天／编程的全局默认模型。`translation route` 只返回执行计划，真正的分节翻译仍由所选 agent／CLI 完成。

## 保留原流程

`do.arxiv-translator` 负责下载 arXiv LaTeX／原文、分节翻译、xeCJK、编译和视觉完整性核验；成功产物归档到：

```text
60_Notes/papers/<Paper_Title>/<Paper_Title>_CN.pdf
```

Zotero skill 负责归档后的下一段：元数据解析、身份核验、增量入库、分类与附件校验。两者边界清楚，不为了入库重新编译已成功的论文，也不覆盖已译 `.tex`。

## Agent 在翻译成功后执行

1. 新翻译开始前先确定 arXiv 的明确 vN，并将 `/abs/IDvN`、`/src/IDvN`、`/pdf/IDvN` 全部固定到同一版本，避免下载期间 latest 变化。已有旧译文若无法追溯就保留未知，不补猜版本号。确认完整原文标题、翻译 PDF 路径；可用本 skill 的 `fetch-original` 保存固定版本原文与 receipt。
2. 使用 `manifest` 命令为一篇论文生成一条记录，`--original` 与 `--translation` 放在同一条记录。
3. 用 `sync --manifest ... --apply` 自动完成非破坏性入库。
4. 必须检查 verification；入库失败不删除翻译产物。再次处理应补齐，不重复建文献。

```bash
TOOL='<skill-dir>/scripts/zotero_library.py'
python3 "$TOOL" manifest \
  --arxiv '<verified-ID-including-vN>' --title '<verified-original-title>' \
  --source-root '/absolute/vault' --source-name DeepOrbit \
  --translation '/absolute/vault/60_Notes/papers/Title/Title_CN.pdf' \
  --original '/absolute/vault/60_Notes/papers/Title/Title_EN.pdf' \
  --translation-date 'YYYY-MM-DD' \
  --translation-revision t1 \
  --out '/private/workdir/manifest.json'
python3 "$TOOL" sync --manifest '/private/workdir/manifest.json' --apply
```

仅在确知时提供 `--translation-date` 和 `--translation-revision`。有本 skill 的固定版本下载记录时增加 `--receipt /private/download-receipts/<sha>.json`，工具会检查原文哈希并记录译文来源。`--original` 可省略；不要引用一个不存在的占位路径。工具不会自动从“最新 arXiv 元数据”推断本地译文的源版本。新字节入库会分配独立归档 rN，旧原文／译文和标注保留。详见 [versions.md](versions.md)。

## 如果未来要把 hook 正式加入 DeepOrbit 仓库

先重新读取它的 `AGENTS.md` 和翻译 skill。按仓库约定同步 skill／命令兼容层、英文与中文 README；不要直接编辑用户 `deeporbit.json`。用 DeepOrbit 的配置接口修改目录语义，保留 Obsidian 目录英文名称和原有笔记作者约定。

目前只是一个可独立发现的全局 skill，agent 可在翻译任务完成后调用。它不会在没有 agent 会话时自动处理文件，也不假装已经改好了 DeepOrbit 的原 skill。
