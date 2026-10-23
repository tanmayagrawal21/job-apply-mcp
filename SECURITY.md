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
- The server never solves CAPTCHAs, creates accounts, or ticks attestations that match `policy.stop_phrases`. It
  enters a verification code only when the user typed it (ADR-0026), confirmed a granted prefill (ADR-0027), or
  minted a consent window with `otp_unattended` from an interactive terminal (ADR-0048); the code is never logged,
  stored or returned. A visible CAPTCHA challenge is always a human handoff, under every setting. Unattended codes are
  taken only from mail your server DKIM/DMARC-verified for the expected sender, with mail settings from the trusted
  user config.
- Residual risk of grants and consent windows: the TTY check and typed phrase stop an agent acting through MCP, not a
  local process with shell access (it can allocate a pseudo-terminal, e.g. `script`, or read the file-fallback HMAC
  key when no OS keychain is available). Do not give agents an unrestricted shell as your OS user if you rely on
  them; install the `keyring` extra so the secret lives in the OS keychain.
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
