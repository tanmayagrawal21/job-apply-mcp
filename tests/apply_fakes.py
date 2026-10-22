"""Small local fakes for apply-engine tests (no real profile/router/gates/store/browser)."""
from __future__ import annotations

import asyncio
import json
from typing import ClassVar

from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import (
    ApplicationForm,
    Decision,
    FieldKind,
    FormField,
    GateResult,
    Posting,
    SubmitResult,
)

POSTING = Posting(ref="greenhouse:acme:1", platform="greenhouse", company="Acme Robotics", title="Software Engineer",
                  url="https://boards.greenhouse.io/acme/jobs/1",
                  description="<p>We are building warehouse robots that cut order errors. You will design and "
                              "build the control software that keeps fleets of robots moving safely.</p>")


def F(key, label, kind=FieldKind.text, required=True, options=None, max_length=None):
    return FormField(key=key, label=label, kind=kind, required=required, options=options or [], max_length=max_length)


FORM = ApplicationForm(ref=POSTING.ref, submit_via=["api", "browser"], fields=[
    F("first_name", "First Name *"), F("last_name", "Last Name"), F("email", "Email"),
    F("resume", "Resume/CV", FieldKind.file),
    F("q_gender", "Gender", FieldKind.select, options=["Male", "Female", "Decline To Self Identify"]),
    F("q_source", "How did you hear about this job?", FieldKind.select, options=["LinkedIn", "Indeed", "Other"]),
    F("q_tool", "Favorite build tool", FieldKind.text, max_length=40),
    F("q_why", "Why Acme Robotics?", FieldKind.textarea),
])


class FakeProfile:
    resume_path = "/tmp/resume.pdf"
    cover_letter_path = None
    VALUES: ClassVar[dict] = {"first_name": "Ada", "last_name": "Rivera", "email": "ada@example.com", "gender": "decline"}

    def lookup(self, key):
        return self.VALUES.get(key)

    def facts(self, topic="", limit=12):
        return ["Built a fleet scheduler in Python at Initech (2023-2025).", "Skills: Python, Rust, SQL"][:limit]


class FakeAdapter:
    name = "greenhouse"

    def __init__(self, form=FORM, posting=POSTING, submit_status="submitted", delay=0.0):
        self.form, self.posting, self.delay = form, posting, delay
        self.submit_status = submit_status
        self.submits: list[tuple] = []
        self.fetches = 0

    def matches(self, url):
        return True

    async def get_posting(self, ref, *, with_description=False):
        self.fetches += 1
        return self.posting

    async def get_form(self, ref):
        return self.form

    async def submit(self, form, answers, files, *, dry_run=True):
        self.submits.append((dict(answers), dict(files), dry_run))
        await asyncio.sleep(self.delay)
        return SubmitResult(ref=form.ref, status="dry_run" if dry_run else self.submit_status, via="api")


class FakeRouter:
    def __init__(self, short=None, prose="x", review=None):
        self.calls: list[tuple[str, str]] = []
        self.short, self.prose, self.review = short, prose, review

    async def available(self, task_class):
        return True

    async def complete(self, prompt, *, task_class="short", system=None, max_tokens=256, json_schema=None):
        self.calls.append((task_class, prompt))
        if task_class == "short":
            return json.dumps(self.short if self.short is not None else {})
        if task_class == "review":
            return json.dumps(self.review or {"pass": True, "violations": [], "corrected": ""})
        return self.prose(prompt) if callable(self.prose) else self.prose


class FakeGates:
    def __init__(self, decision=Decision.submit, reasons=None):
        self.decision, self.reasons = decision, reasons or []

    async def evaluate(self, posting, form):
        return GateResult(decision=self.decision, reasons=self.reasons)


class FakeStore:
    def __init__(self, seen=False):
        self._seen, self.records = seen, []
        self._locks: dict[str, asyncio.Lock] = {}

    async def seen(self, company, title, *, within_days=180):
        return self._seen or any(r.status == "submitted" and c == company and t == title
                                 for r, c, t in self.records)

    async def record(self, result, *, company, title):
        self.records.append((result, company, title))

    def company_lock(self, company):
        return self._locks.setdefault(company, asyncio.Lock())


class FakeDriver:
    name = "fake"

    def __init__(self, status="submitted", detail=None, delay=0.0):
        self.status, self.detail, self.delay = status, detail, delay
        self.calls = []

    async def fill_and_submit(self, apply_url, form, answers, files, *, dry_run=True):
        self.calls.append((apply_url, dict(answers), dict(files), dry_run))
        await asyncio.sleep(self.delay)
        return SubmitResult(ref=form.ref, status="dry_run" if dry_run else self.status, via="browser",
                            detail=self.detail)


def settings(**over) -> Settings:
    s = Settings()
    s.dry_run = False
    for k, v in over.items():
        sect, _, key = k.partition("__")
        setattr(getattr(s, sect), key, v)
    return s
