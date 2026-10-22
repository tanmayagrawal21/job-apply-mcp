# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private vulnerability reporting
("Security" tab, "Report a vulnerability") on https://github.com/tanmayagrawal21/job-apply-mcp. Include the
version (`job-apply-mcp --version`), what you did and what happened. You will get an acknowledgement within
7 days and a fix or mitigation plan within 30 days for confirmed issues. Credit is given unless you prefer
otherwise.

In scope: bypassing dry-run, approval tokens, controls or hard stops; leaking profile data, credentials or
tokens; the HTTP transport's authentication; unsafe file or command handling. Out of scope: the behaviour of
third-party job sites, and misconfiguration that disables the safeguards (for example `dry_run = false` with
`apply.auto_approve = true`).

Supported versions: the latest release and `main`.

## Security model in brief

- Defaults are safe: `dry_run = true`; real submission needs an approval token bound to the exact answers.
- The server never solves CAPTCHAs, creates accounts, enters verification codes, or ticks attestations that
  match `policy.stop_phrases`.
- HTTP mode binds 127.0.0.1; a non-loopback bind requires `JOB_APPLY_MCP_TOKEN` (compared in constant time).
  Put TLS in front of it (reverse proxy) for any network use. The token is never accepted as a CLI flag.
- Secrets (API keys, IMAP passwords) are read from the environment or OS keychain, never from config files.
- Page text is treated as data; the control layer screens it for injected instructions.

## Supply-chain policy

- Core dependencies stay minimal (`mcp`, `httpx`, `pydantic`); heavy libraries are optional extras imported
  lazily. Every new dependency needs a written justification in its PR (ADR-0004).
- `uv.lock` is committed and CI installs with `--locked`. `tool.uv.exclude-newer` blocks packages published in
  the last 14 days; it is bumped only in a dedicated PR after reviewing the lockfile diff.
- GitHub Actions use pinned major versions and read-only permissions. A CI privacy scan blocks personal data and
  secrets from entering the repository (ADR-0018).
- Releases are built from tagged commits; verify what you install with `pip download job-apply-mcp` and compare
  hashes against the GitHub release when published.
