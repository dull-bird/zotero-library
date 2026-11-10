# Repository guidance

This repository is the source for the `zotero-library` Agent Skill and its stdlib-only CLI. Read `SKILL.md` and the relevant `references/` policy before changing behavior.

## Invariants

- Never write SQL to the live Zotero database. Read stable DB+WAL snapshots; use the authorized native local API for writes.
- Never bypass local API preferences or click permission dialogs. Keep credentials internal and owner-only.
- Preserve existing important metadata, PDFs, annotations, collections and notes. Destructive actions and broad overwrite approvals are out of scope.
- Work identity, selected metadata edition, attachment source version, translator revision, archive rN and Zotero clientVersion are distinct.
- A new source version must not silently relabel an old PDF or translation. Unknown provenance remains unknown.
- The top-level managed collection aggregates papers as well as the classification leaves. A collection is a logical membership, not a file copy.
- Check per-object batch results and stored file bytes. Reconcile uncertain writes rather than blind retries.

## Validation

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts
```

Tests must remain offline and not depend on a real Zotero installation. Add regressions for behavior changes. Use `TESTING.md` to distinguish mocked tests from live acceptance; do not claim validation on untested versions/platforms.

## Publishing and privacy

Commit code, synthetic tests and generic documentation only. Never commit PDFs, SQLite/WAL files, authorization, real manifests/plans, receipts, caches, journals, backups, private repository references or personal filesystem paths. Review the full staged tree before pushing. Do not infer that a public repository grants an unspecified software license.

Keep the core dependency-free and avoid adding a daemon or MCP layer merely to wrap a CLI command. Translation integration is an opt-in post-processing guide, not permission to modify another repository or run untrusted TeX.
