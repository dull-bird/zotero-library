# Validation scope

## Automated tests

```bash
python3 -m unittest discover -s tests -v
```

The suite has **77 offline tests**, checked locally on Python 3.9 and 3.12. CI uses Ubuntu 22.04 for Python 3.9 (no prebuilt package on newer runners) and the latest Ubuntu runner for Python 3.12. It uses synthetic metadata/PDF bytes, temporary SQLite files, mock clients and a loopback HTTP contract server. It does not access a real library or require Zotero to be installed.

Coverage includes strong-identifier deduplication, title/author/DOI checks, protected fields, multipart stored dates, DB+WAL snapshots, stale versions, per-object failures inside HTTP 200, native three-phase upload contracts, partial recovery, idempotency, source/version separation, archive rN, receipts, and retaining old attachments. The top-level collection is tested as an aggregate, not an empty folder.

## Live native acceptance — 2026-11-10

Environment: **Zotero 10.0.8, schema 44, macOS arm64**. The user enabled the API and personally approved persistent local authorization. All changes used native local APIs; private database backups, plans and journals were retained locally.

| Stage | Observed result |
|---|---|
| Single existing-paper pilot | Existing record updated, missing stored attachment added, fields/parent/hash verified; no duplicate paper |
| Two-paper batch | Two new paper records and their stored PDFs verified, including source-verified metadata |
| Repeat the small batch | Item/attachment/note/collection counts unchanged; **zero non-GET requests** |
| Full incremental ingestion | **17 paper records and 17 stored PDF attachments** verified; no blocked records or selected-edition metadata gaps |
| Existing content | All pre-existing item keys retained; original registered PDF retained with identical bytes |
| Aggregate collection | Top-level managed collection directly contains all 17 paper records |
| Repeat full plan | All 17 papers/PDFs verified again; **zero non-GET requests** |
| Stored PDF parsing | All 17 PDFs opened with PDFKit, had nonzero page counts and first-page text |

The PDFKit probe emitted a generic CoreGraphics warning but no document-open failures. Parsing and hash equality do not substitute for a visual/content audit of every page.

## What this does not establish

- End-to-end support for every Zotero release, operating system, proxy or library configuration.
- Live testing of every partial-failure, timeout, concurrent-edit or authorization-denial branch; these have offline coverage, not deliberate destructive fault injection into a user's library.
- The exact original source version or correctness/completeness of legacy translations. Unknown source versions remained unknown.
- Complete publisher metadata when authoritative sources do not supply it. `metadata_complete_for_selected_edition` covers selected, verified fields, not invented volume/pages/DOIs.
- Live coverage of every original-download or publisher-edition selection path; relevant tests include mocked public endpoints.
- A daemon, automatic cloud sync, universal version-history crawler or a separately implemented MCP Server.

Raw acceptance records are private because they contain library identifiers and filesystem paths. This document reports aggregate results only; no database, PDF, token or private repository content is published.

## Repeat on another installation

1. Run `doctor` and `source-audit`; review the installed API/schema and make a small plan.
2. Enable the native API and request authorization, leaving the dialog to the human.
3. Apply one paper; inspect actual metadata, collections, attachment parent, stored SHA-256 and PDF readability.
4. Re-read the library, generate a two-paper batch, apply and repeat it. Verify counts and that repeat writes are unnecessary.
5. Re-plan the remainder; stop on conflicts, partial results or unknown outcomes. Do not restore/delete library objects automatically.
