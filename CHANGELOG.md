# Changelog

## 1.1.1 — 2026-11-10

- Complete live native write acceptance on Zotero 10.0.8 / schema 44 / macOS arm64: one-paper pilot, two-paper batch and full 17-paper ingestion with 17 stored PDFs.
- Reapplying the small and full plans issues zero write requests; existing items and the original registered PDF remain intact.
- Make the top-level managed collection directly aggregate papers, alongside topic/type/year/source leaves.
- Verify stored PDF hashes and PDFKit readability; add the aggregate-collection regression, bringing the offline suite to 77 tests.
- Prepare a standalone public repository with generic documentation, CI and explicit privacy boundaries; exclude library data and credentials.

## 1.1.0 — 2026-11-10

- Separate work identity, selected metadata edition, per-attachment source version, translator revision, automatic archive rN and Zotero clientVersion.
- Preserve explicit arXiv vN in manifests and metadata queries; never silently fall back from a pinned version to latest.
- Combine versioned repository PDFs under one paper; keep different bytes as separate stored attachments without overwriting old files or annotations.
- Recover archive history from structured provenance notes; same bytes reuse an archive revision.
- Add read-only update reports, optional fresh queries, and Crossref declared-version/relation reporting.
- Add explicitly pinned original PDF downloads with streaming, hashes and local receipts. External receipts are treated as declarations unless a matching local record exists.
- Add version and archive regression tests; total 76 offline/contract tests pass.

## 1.0.0 — 2026-11-10

- Initial portable Agent Skill with stdlib-only native Zotero 10 client.
- Source/schema inspection, stable DB+WAL read-only snapshots and multipart date decoding.
- Official arXiv resolution, strict Crossref edition selection, additive multi-dimensional collections.
- Idempotent plan/apply/verify with server ID and object-version checks, upload phases, hashes, private backups and journals.
- DeepOrbit post-translation integration guide without modifying the upstream repository.

Live acceptance results and their limits are documented in [TESTING.md](TESTING.md). Validation on one installation is not a guarantee for other Zotero versions or every failure mode.
