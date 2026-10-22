"""Structured proposal: spec, files to touch, test plan, risk, ADR draft. Persisted per id."""
from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .classify import risk_for, slugify

PKG = "src/job_apply_mcp"


@dataclass
class FeatureProposal:
    id: str
    kind: str
    description: str
    slug: str
    risk: str
    spec: str
    files: list[str]
    test_plan: list[str]
    adr_draft: str
    context: dict = field(default_factory=dict)

    @property
    def branch_suffix(self) -> str:
        return f"{self.id}-{self.slug}"


def new_id() -> str:
    return time.strftime("%Y%m%d") + "-" + secrets.token_hex(2)


def _name_guess(description: str) -> str:
    m = re.search(r"\b([A-Za-z][\w-]{2,})\s+(?:adapter|platform|ats)\b", description) or \
        re.search(r"(?:adapter|support|platform)\s+for\s+([A-Za-z][\w-]{2,})", description, re.IGNORECASE)
    return slugify(m.group(1), 20).replace("-", "_") if m else "new_platform"


def _files_and_tests(kind: str, description: str) -> tuple[list[str], list[str]]:
    if kind == "platform_adapter":
        n = _name_guess(description)
        return ([f"{PKG}/platforms/{n}.py", "tests/test_platform_" + n + ".py", "docs/adr/NNNN-" + n + "-adapter.md"],
                ["Implement PlatformAdapter Protocol; self-register in core/registry.platforms",
                 "Offline tests with respx fixtures for list/get/form; no live network",
                 "Add the name to Settings.platforms_enabled default only if tested"])
    if kind == "control":
        return ([f"{PKG}/policy/", "tests/test_policy_*.py", "docs/adr/NNNN-control.md"],
                ["Table-driven gate tests including the negative case", "Confirm stop_phrases and dry_run defaults unchanged"])
    if kind == "config":
        return ([f"{PKG}/config.py", "tests/test_core.py", "docs/adr/0003-configuration.md"],
                ["Default preserves current behaviour", "Env override test (JOB_APPLY__SECTION__KEY)"])
    if kind == "new_tool":
        return ([f"{PKG}/server.py", f"{PKG}/services.py", "tests/test_tools_*.py"],
                ["Tool returns compact JSON under response_budget_chars", "Pagination/fields opt-in per ADR-0005"])
    return (["(locate via relevant_files in the context pack)", "tests/test_regression_*.py"],
            ["Write a failing regression test first, then fix", "Run the full suite"])


def build(description: str, kind: str, context: dict, pid: str | None = None) -> FeatureProposal:
    pid = pid or new_id()
    files, plan = _files_and_tests(kind, description)
    files = list(dict.fromkeys(files + context.get("relevant_files", [])[:3]))
    risk = risk_for(kind, description)
    spec = (f"Request ({kind}): {description.strip()}\n\nConstraints: follow ADR-0001 modularity (self-register, "
            "no server.py edits beyond wiring), ADR-0004 (no new core dependency), ADR-0005 (token-frugal outputs), "
            "dry_run stays default true, never solve CAPTCHAs/create accounts/tick stop-phrase attestations, "
            "no personal data or secrets.")
    adr = (f"# ADR-NNNN: {description.strip()[:70]}\n\nStatus: proposed\n\n## Context\n(why this change)\n\n"
           "## Decision\n(what the change does)\n\n## Consequences\n(trade-offs)\n\n"
           f"## Verify / revert\nVerify: `uv run pytest -q`. Revert: `git revert <squash sha>`. Risk: {risk}.\n")
    return FeatureProposal(pid, kind, description, slugify(description), risk, spec, files, plan, adr, context)


def to_markdown(p: FeatureProposal) -> str:
    ctx = p.context
    tmpl = ctx.get("template") or {}
    return "\n".join([
        f"# Proposal {p.id} ({p.kind}, risk: {p.risk})", "", "## Spec", p.spec, "",
        "## Files to touch", *[f"- {f}" for f in p.files], "",
        "## Test plan", *[f"- {t}" for t in p.test_plan], "",
        "## Context", f"Protocols: {', '.join(ctx.get('protocols', []))}",
        "Relevant: " + ", ".join(ctx.get("relevant_files", [])),
        "ADRs: " + "; ".join(ctx.get("adrs", [])),
        "Run evidence: " + ("; ".join(ctx.get("evidence", [])) or "none"),
        f"Template: {tmpl.get('path', 'n/a')}", "",
        "## ADR draft", p.adr_draft, ""])


def save(p: FeatureProposal, root: Path) -> Path:
    d = root / p.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "proposal.json").write_text(json.dumps(asdict(p), indent=1))
    (d / "proposal.md").write_text(to_markdown(p))
    return d
