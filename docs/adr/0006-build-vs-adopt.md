# ADR-0006: Build a thin core; depend on a few permissive projects behind interfaces

Status: accepted · Date: 2026-10-22

## Context
We surveyed existing open-source work before building. Findings were as of 2026-10-22, with star counts
approximate.

| Project | License | Verdict | Why |
|---|---|---|---|
| JobSpy (speedyapply/JobSpy, ~4.5k★) | MIT | **Depend** (optional extra) | Scrapes LinkedIn, Indeed, Glassdoor and ZipRecruiter. Does not cover Greenhouse, Lever or Ashby. |
| browser-use (~119k★) | MIT | **Depend** (optional extra) | Mature LLM browser-agent loop that supports Ollama. It is the generic form-filler fallback. |
| LiteLLM (~62k★) | MIT core | **Optional, pinned ≥1.83.0** | PyPI 1.82.7/1.82.8 were malicious (Mar 2026 supply-chain attack). The core router speaks the OpenAI-compatible API directly instead. |
| RouteLLM | Apache-2.0 | Borrow ideas | Trained weak-to-strong routers are tuned for chat. Form filling needs cheap rule-based escalation. |
| Skyvern | AGPL-3.0 | Borrow ideas | Schema-first extraction is useful, but the vision stack is heavy for 16 GB and the license is AGPL. |
| Stagehand | MIT (TypeScript) | Borrow ideas | The act/observe/extract split and action caching. |
| Playwright MCP | Apache-2.0 | Borrow ideas | Accessibility-tree snapshots with depth limits. It is too chatty to be our layer. CVE-2025-9611 means pinning ≥0.0.40 if run as a sidecar. |
| ApplyPilot, AIHawk | AGPL-3.0 | Avoid forking | Young or abandoned, untested, and they carry platform-ban risk. Their pipeline stages informed our tool set. |
| Existing job MCP servers | various | Borrow ideas | Read-only board wrappers. The hash-bound human approval step is a good idea. |

## Decision
Build our own thin core (MCP layer on the official Python SDK, adapters for the public ATS JSON APIs,
router, policy, store). Integrate JobSpy, browser-use and (optionally) LiteLLM only behind the
`PlatformAdapter`, `BrowserDriver` and `LLMProvider` Protocols, as optional extras, so any of them can
be removed by deleting one module.

Borrowed designs:
- Approval tokens. `plan_application` returns a hash of the exact answers, and `submit_application`
  requires that hash unless `auto_approve` is configured.
- Act/observe/extract style browser primitives.
- Rule-based LLM escalation (confidence, validation failure, field kind).

## Verify / revert
Verify: `pip show` / `uv tree` show no AGPL dependency, and optional extras are absent from the core
install. Revert: drop the extra and its adapter module.
