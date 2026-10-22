# ruff: noqa: BLE001
"""Generic LLM-driven BrowserDriver for sites without a deterministic filler (ADR-0012). Extra: `agent`.

Wraps the optional `browser-use` agent loop, configured with the router's local model (Ollama), a step cap
and the same stop rules as the Playwright driver. It is the slowest and least predictable path, so the
engine uses it only when the primary driver reports `unsupported_site`.
Because an LLM agent cannot be guaranteed to stop before the final click, dry runs do NOT drive the agent
unless `browser.agent_dry_run_fill` is set.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..config import Settings
from ..core import registry
from ..core.models import ApplicationForm, SubmitResult

RULES = """HARD RULES (never break):
- Never solve or interact with a CAPTCHA / bot check; never enter an email or SMS verification code.
- Never create an account or set a password. If a sign-in/account wall appears, stop.
- Never tick or accept any statement that you personally wrote the answers or did not use AI. Stop instead.
- Fill ONLY the fields listed below with EXACTLY the given values. Do not invent answers. If a required field
  is not listed, stop.
- Upload files last. Click the final submit button at most ONCE. If no confirmation appears, stop; never retry.
- Finish by replying with a single line: RESULT: {"status": "submitted|needs_human|roadblock|duplicate",
  "detail": "<short reason>"}"""


def build_task(url: str, form: ApplicationForm, answers: dict[str, Any], files: dict[str, str], dry_run: bool) -> str:
    labels = {f.key: f.label for f in form.fields}
    lines = [f"- {labels.get(k, k)}: {json.dumps(v, ensure_ascii=False)}" for k, v in answers.items()]
    lines += [f"- {labels.get(k, k)} (file upload): {p}" for k, p in files.items()]
    end = ("Fill the form but DO NOT click the final submit; report status needs_human with detail 'filled'."
           if dry_run else "Then submit the application once.")
    return f"Open {url} and complete the job application form.\n{RULES}\nFIELDS:\n" + "\n".join(lines) + f"\n{end}"


def parse_result(text: str, ref: str) -> SubmitResult:
    m = re.search(r"RESULT:\s*(\{.*\})", text or "", re.DOTALL)
    if not m:
        return SubmitResult(ref=ref, status="needs_human", via="agent",
                            detail="agent gave no RESULT line; check manually and do not retry")
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return SubmitResult(ref=ref, status="needs_human", via="agent", detail="unparseable agent result")
    status = d.get("status") if d.get("status") in ("submitted", "needs_human", "roadblock", "duplicate") \
        else "needs_human"
    return SubmitResult(ref=ref, status=status, via="agent", detail=str(d.get("detail", ""))[:200])


class BrowserUseDriver:
    name = "browser_use"

    def __init__(self, settings: Settings, llm: Any = None):
        self.s, self.b = settings, settings.browser
        self._llm = llm

    def _make_llm(self):
        if self._llm is not None:
            return self._llm
        tier = self.s.llm.tiers.get(self.b.agent_tier) or next(iter(self.s.llm.tiers.values()))
        host = self.s.llm.providers.get("ollama", {}).get("base_url", "http://127.0.0.1:11434")
        try:
            from browser_use import ChatOllama
        except ImportError:
            from browser_use.llm import ChatOllama  # type: ignore[no-redef]
        return ChatOllama(model=tier.model, host=host)

    async def fill_and_submit(self, apply_url: str, form: ApplicationForm, answers: dict[str, Any],
                              files: dict[str, str], *, dry_run: bool = True) -> SubmitResult:
        ref = form.ref
        if dry_run and not self.b.agent_dry_run_fill:
            return SubmitResult(ref=ref, status="dry_run", via="agent",
                                detail="generic agent not run in dry-run (cannot guarantee it stops before submit)")
        try:
            from browser_use import Agent
        except ImportError:
            return SubmitResult(ref=ref, status="needs_human", via="agent",
                                detail="install the 'agent' extra (browser-use) for unknown sites")
        try:
            kwargs: dict[str, Any] = {"task": build_task(apply_url, form, answers, files, dry_run),
                                      "llm": self._make_llm(), "available_file_paths": list(files.values())}
            history = await Agent(**kwargs).run(max_steps=self.b.agent_max_steps)
            text = history.final_result() if hasattr(history, "final_result") else str(history)
            return parse_result(text or "", ref)
        except Exception as e:
            return SubmitResult(ref=ref, status="error", via="agent", detail=f"{type(e).__name__}: {e}"[:200])


@registry.browser_drivers.register("browser_use")
def _make(settings: Settings, **_: Any) -> BrowserUseDriver:
    return BrowserUseDriver(settings)
