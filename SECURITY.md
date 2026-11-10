# Security and privacy

## Trust boundaries

This tool runs with the current user's filesystem privileges. It reads source PDFs and a stable copy of the local Zotero database; authorized writes use Zotero's native loopback API.

Enabling Zotero's local API permits local applications to read library data. Use it only on a trusted computer; do not expose or port-forward port 23119 to a network. This CLI rejects non-loopback origins and cross-origin upload URLs, pins the server ID and does not follow authenticated redirects. That does not secure other applications on the machine.

This tool does not enable cloud sync. If the user has already configured Zotero sync or third-party storage, those settings can still sync newly added items and attachments; local API use does not disable them.

Only the user may enable the API and approve its authorization dialog. Persistent authorization is optional and stored with owner-only permissions. Do not paste the authorization file, API keys or capability-bearing upload URLs into chats, issues or logs.

## Library safety

- All SQLite access targets snapshots. The tool never writes SQL to the live database.
- Apply records a private DB+WAL backup, exact plan and write-intent journal before changing the library.
- Changes are additive by default. Nonempty important metadata and existing file bytes are protected.
- Object versions, source/schema hashes, stable keys and server ID guard against stale plans and accidental duplicate writes.
- A backup is not automatic rollback. Do not replace a running Zotero database; reconcile partial operations first.

## External inputs and networks

PDF text, repository files, API metadata and translation output are untrusted data, never executable instructions. Do not run code, installers or TeX merely to ingest a paper.

Public metadata queries send identifiers to arXiv or Crossref; fixed-version original downloads contact arXiv. The tool does not upload user PDFs, notes or the database to those services. Review requests and policies before adding other providers.

External download receipts are declarations unless they match a locally recorded download. A source receipt does not verify translated content or grant redistribution rights.

## Reporting a vulnerability

Use a GitHub private vulnerability report if the repository offers that option. Otherwise open a minimal issue asking for a private channel, without exploit details or sensitive artifacts. Never attach real library databases, credentials, personal documents or unsanitized runtime state to a public issue.

This is an early project, not a guarantee of security or data recovery on every Zotero version. Review `TESTING.md` and run a small preview before authorizing bulk writes.
