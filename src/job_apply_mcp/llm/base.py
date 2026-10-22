"""Shared LLM types: completion result, task description, errors, and a tiny JSON-schema validator."""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


class ProviderError(Exception):
    """A provider call failed. `retryable` means a retry or backoff may help (timeouts, 429, 5xx)."""

    def __init__(self, msg: str, *, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


class ProviderUnavailable(ProviderError):
    pass


class ClientTask(BaseModel):
    """Returned instead of text when the calling MCP agent should write the answer itself."""

    field_key: str = ""
    instruction: str
    system: str | None = None
    max_tokens: int = 512
    json_schema: dict | None = None

    def compact(self) -> dict[str, Any]:
        d: dict[str, Any] = {"key": self.field_key, "do": self.instruction, "max_tokens": self.max_tokens}
        if self.system:
            d["system"] = self.system
        if self.json_schema:
            d["schema"] = self.json_schema
        return d


class ClientTaskRequired(Exception):
    """Raised by the `client` provider when no sampling channel exists: the caller must write the text."""

    def __init__(self, task: ClientTask):
        super().__init__("client must generate")
        self.task = task


@dataclass
class Completion:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


def estimate_tokens(s: str) -> int:
    return max(1, len(s) // 4)


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(s: str) -> str:
    return _THINK.sub("", s).strip()


Sampler = Callable[..., Awaitable[str]]


class Task(BaseModel):
    """What the router needs to know to pick a tier. Everything optional except the prompt."""

    prompt: str
    system: str | None = None
    key: str = ""                       # form field key, for ClientTask
    question: str = ""                  # the visible question/label, scanned for essay keywords
    task_class: str | None = None       # "field" | "narrative" | "review"; None = auto-detect
    field_kind: str | None = None       # a core.models.FieldKind value
    expected_chars: int = 0             # expected answer length
    company_specific: bool = False      # needs reasoning about the specific employer/role
    prior_failures: int = 0             # validation failures already seen upstream
    json_schema: dict | None = None
    max_tokens: int | None = None       # override the tier's max_tokens
    temperature: float = 0.2
    validator: Any = Field(default=None, exclude=True)  # Callable[[str], str | None]: error message or None


class LLMResult(BaseModel):
    ok: bool
    text: str = ""
    data: Any = None
    client_task: ClientTask | None = None
    tier: str = ""
    provider: str = ""
    model: str = ""
    task_class: str = "field"
    score: int = 0
    reasons: list[str] = Field(default_factory=list)
    escalations: int = 0
    error: str = ""

    def compact(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": self.ok, "class": self.task_class, "tier": self.tier, "via": f"{self.provider}:{self.model}"}
        if self.client_task:
            d["client_task"] = self.client_task.compact()
        elif self.data is not None:
            d["data"] = self.data
        else:
            d["text"] = self.text
        if self.escalations:
            d["esc"] = self.escalations
        if self.error:
            d["error"] = self.error
        return d


def extract_json(text: str) -> Any:
    t = strip_think(text).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        i, j = t.find(open_), t.rfind(close)
        if 0 <= i < j:
            try:
                return json.loads(t[i : j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON found in output")


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def validate_schema(value: Any, schema: dict, path: str = "$") -> str | None:
    """Minimal JSON-schema subset (type, enum, required, properties, items). Returns an error or None."""
    t = schema.get("type")
    if t:
        ok = (
            (isinstance(value, (int, float)) and not isinstance(value, bool)) if t == "number"
            else (isinstance(value, int) and not isinstance(value, bool)) if t == "integer"
            else isinstance(value, _TYPES.get(t, object))
        )
        if t == "boolean" and not isinstance(value, bool):
            ok = False
        if not ok:
            return f"{path}: expected {t}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: not in enum {schema['enum']}"
    if isinstance(value, dict):
        for r in schema.get("required", []):
            if r not in value:
                return f"{path}: missing '{r}'"
        for k, sub in schema.get("properties", {}).items():
            if k in value and (err := validate_schema(value[k], sub, f"{path}.{k}")):
                return err
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            if err := validate_schema(v, schema["items"], f"{path}[{i}]"):
                return err
    return None


@dataclass
class CallStats:
    calls: int = 0
    errors: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    total_ms: float = 0.0
    last_error: str = ""

    def view(self) -> dict[str, Any]:
        return {"calls": self.calls, "errors": self.errors, "tok_in": self.tokens_in,
                "tok_out": self.tokens_out, "avg_ms": round(self.total_ms / self.calls) if self.calls else 0,
                **({"last_error": self.last_error} if self.last_error else {})}


