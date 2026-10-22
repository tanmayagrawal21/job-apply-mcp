# ADR-0016: Mail-confirmation dedupe (read-only)

Status: accepted · Date: 2026-10-22

## Context
Users also apply by hand, via LinkedIn/Handshake, or from other tools. Those applications are invisible to the
store, so the engine could apply twice. The user's own "application received" emails are a reliable record.
Mailboxes are highly sensitive, so scope, credentials and retention must be minimal.

## Decision
- **Read-only scope.** Nothing is ever sent, moved, flagged, deleted or marked read. IMAP uses `select(readonly=True)`
  (EXAMINE), `SEARCH` and `BODY.PEEK[]<0.16384>` only; mbox/eml files are opened for reading; client mode is data
  passed to a tool. Tests assert no other IMAP command is reachable.
- **Data minimization.** A `Message` carries sender, subject, date and a <=1200 char snippet, in memory only. The
  store keeps company, title, platform, day, sender *domain* and confidence; never bodies, addresses or snippets.
  Tool outputs are counts plus at most `ingest.max_new_shown` new matches.
- **Sources** behind `MailSource.fetch(since)`: `client` (default; the calling agent supplies summaries through the
  `ingest_confirmations` tool, so the server holds no credentials), `mbox` (file or directory of `.eml`; offline),
  `imap` (date-range SEARCH plus optional FROM/SUBJECT filters, host/folder in config).
- **Credentials** only from environment variables (names configurable, defaults `JOB_APPLY_IMAP_USER` /
  `JOB_APPLY_IMAP_PASSWORD`) or the OS keychain via the optional `keyring` extra (lazy import). The config schema
  has no password or username field.
- **Parsing** (`ingest/confirmations.py`): a message must match a positive subject/body pattern and no negative one
  (rejections, job alerts, reminders). Sender domain identifies the platform (Greenhouse, Lever, Ashby, Workday,
  iCIMS, SmartRecruiters, Taleo, SuccessFactors, LinkedIn, Handshake; otherwise "employer"). Company/role come from
  ordered regexes with named groups, with the sender display name as company fallback. Confidence = platform
  (0.3) + subject (0.3) + body (0.1) + company (0.15, 0.08 if from sender name) + role (0.15). Built-in
  `patterns.toml` is extended by `ingest.patterns_file` (lists append, platforms merge by name): new senders need no code.
- **Store integration.** Only the Store protocol (`seen`, `record`) is used. A confirmation with company, title,
  confidence >= `min_confidence` and a date within `policy.dedupe_days` that is not already `seen()` is recorded as
  `submitted`, `via="email"`, ref `mail:<sha256(sender+subject+day)[:20]>` (idempotent; re-ingesting is a no-op
  counted as `known`). `seen()` then blocks re-applying, so the apply engine's pre-submit dedupe benefits unchanged.
- **Tools:** `ingest_confirmations(messages, preview)` and `sync_confirmations(since_days, source, preview)`.

## Consequences
`record()` stamps `created_at` as ingestion time, so the dedupe window for an email-sourced row starts at ingestion,
slightly later than the email date; mails older than `dedupe_days` are skipped. Messages lacking a parsable
role are reported (`matched` minus `new`) but not recorded, since an empty title cannot match. Regex extraction
is heuristic: tune with a user patterns file. Fuzzy company/title matching is the store's (ADR-0010).

## Verify / revert
`uv run pytest tests/test_ingest.py -q` (fictional fixtures in `tests/fixtures/mail/`). Revert: delete `ingest/`,
the two lines in `server.py`, `IngestCfg` in `config.py`, and the `keyring` extra.
