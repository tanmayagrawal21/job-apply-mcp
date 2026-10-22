# FAQ

**Will it submit applications behind my back?** No. `dry_run = true` is the default, and real submissions also
need the approval token from `plan_application`, which is bound to a hash of the exact answers. Controls such as
the daily quota and the single-submit guard run on every action.

**Does it need a GPU or a local model?** No. Without a local runtime, `init` configures every tier to hand the
work to your MCP client's model. A local model (Ollama, LM Studio, mlx_lm.server) just keeps routine form fields
off the cloud and cheap.

**Which clients work?** Any MCP client. See [clients](clients/README.md).

**Does it work on Windows and Linux?** Yes. Config lives in `%APPDATA%\job-apply-mcp` on Windows and
`~/.config/job-apply-mcp` elsewhere; CI runs on Ubuntu, macOS and Windows. The MLX and Hugging Face extras are
optional and never imported unless configured.

**Where is my data?** Profile and config in the config directory, applications, caches and audit log in the
data directory (`paths.data_dir`). Nothing leaves your machine except requests to job boards and to whichever LLM
provider you configure. `llm.allow_cloud = false` keeps all model calls local.

**Can I run it on a server?** Yes, with `--transport http` and `JOB_APPLY_MCP_TOKEN`; see the Dockerfile. Browser
automation wants a visible, signed-in browser, so it is usually best run on your own computer.

**It said `needs_human`.** The tool met something it must never do alone (CAPTCHA, verification code, account
creation, a no-AI attestation, an unanswerable required field). Open the page and finish that step yourself.

**`doctor` says the browser is missing.** Install Chrome, or `pip install 'job-apply-mcp[browser]'` and
`playwright install chromium`. Platforms with public APIs (Greenhouse, Lever, Ashby, SmartRecruiters) work without
a browser for searching and form inspection.

**How do I update?** `uvx` fetches the latest each run if you pin nothing; `pipx upgrade job-apply-mcp` otherwise.
Read the [CHANGELOG](../CHANGELOG.md) first; `doctor` validates your config afterwards.

**Is this legal / allowed by the job sites?** Check each site's terms and your own obligations. You are
responsible for what is submitted in your name; that is why dry-run, review and approval tokens exist.
