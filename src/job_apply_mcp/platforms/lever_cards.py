"""Lever custom-question "cards" from the hosted apply page (ADR-0031).

Each card is a hidden `cards[<uuid>][baseTemplate]` input whose value is JSON (`fields`: type/text/required/options);
the matching inputs are named `cards[<uuid>][field<N>]` where N is the field's index. JSON is preferred; if it is
missing or malformed the visible DOM (`li.application-question`) is parsed instead.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

from ..core.models import FieldKind, FormField

_KIND = {"text": FieldKind.text, "textarea": FieldKind.textarea, "multiple-choice": FieldKind.select,
         "dropdown": FieldKind.select, "multiple-select": FieldKind.multiselect, "date": FieldKind.date,
         "number": FieldKind.number}
_NAME = re.compile(r"^cards\[([^\]]+)\]\[(field\d+)\]$")
_BASE = re.compile(r"^cards\[([^\]]+)\]\[baseTemplate\]$")


def _label(s: str) -> str:
    return re.sub(r"\s+", " ", s).replace("✱", "").strip()


class _Page(HTMLParser):
    """Collects baseTemplate JSON strings and, per `application-question` item, its label and card inputs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.templates: dict[str, str] = {}
        self.items: list[dict] = []
        self._li: dict | None = None
        self._depth = 0
        self._in_text = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        name = a.get("name") or ""
        if tag == "input" and _BASE.match(name):
            self.templates[_BASE.match(name).group(1)] = a.get("value") or ""
        if tag == "li" and "application-question" in (a.get("class") or "").split():
            self._li = {"label": "", "inputs": []}
            self.items.append(self._li)
            self._depth = 1
            return
        if self._li is None:
            return
        if tag == "li":
            self._depth += 1
        if tag == "div" and "text" in (a.get("class") or "").split() and not self._li["label"] and not self._in_text:
            self._in_text = 1
        elif self._in_text and tag == "div":
            self._in_text += 1
        if tag in ("input", "select", "textarea") and _NAME.match(name):
            self._li["inputs"].append({"tag": tag, "type": (a.get("type") or "text") if tag == "input" else tag,
                                       "name": name, "value": a.get("value") or "", "required": "required" in a})
        if tag == "option" and self._li["inputs"] and self._li["inputs"][-1]["tag"] == "select":
            self._li["inputs"][-1].setdefault("opts", []).append(a.get("value") or "")

    def handle_endtag(self, tag):
        if self._li is None:
            return
        if self._in_text and tag == "div":
            self._in_text -= 1
        if tag == "li":
            self._depth -= 1
            if self._depth == 0:
                self._li = None

    def handle_data(self, data):
        if self._li is not None and self._in_text:
            self._li["label"] += data


def _from_json(templates: dict[str, str]) -> list[FormField]:
    out: list[FormField] = []
    for cid, raw in templates.items():
        try:
            fields = json.loads(raw).get("fields") or []
        except (ValueError, AttributeError):
            return []                                   # malformed -> let the DOM path decide for the whole page
        for i, d in enumerate(fields):
            if not isinstance(d, dict) or not d.get("text"):
                continue
            opts = [str(o.get("text", "")) for o in d.get("options") or [] if isinstance(o, dict) and o.get("text")]
            kind = _KIND.get(str(d.get("type")), FieldKind.text)
            out.append(FormField(key=f"cards[{cid}][field{i}]", label=_label(d["text"]), kind=kind,
                                 required=bool(d.get("required")), options=opts if kind in
                                 (FieldKind.select, FieldKind.multiselect) else []))
    return out


def _from_dom(items: list[dict]) -> list[FormField]:
    out: list[FormField] = []
    seen: set[str] = set()
    for it in items:
        ins = it["inputs"]
        if not ins or ins[0]["name"] in seen:
            continue
        first = ins[0]
        seen.add(first["name"])
        t = first["type"]
        if t == "radio":
            kind, opts = FieldKind.select, [i["value"] for i in ins]
        elif t == "checkbox":
            kind, opts = FieldKind.multiselect, [i["value"] for i in ins]
        elif t == "select":
            kind, opts = FieldKind.select, [o for o in first.get("opts", []) if o]
        else:
            kind, opts = (FieldKind.textarea if t == "textarea" else FieldKind.text), []
        out.append(FormField(key=first["name"], label=_label(it["label"]), kind=kind, options=opts,
                             required=any(i["required"] for i in ins) or "✱" in it["label"]))
    return out


def parse_cards(html: str) -> list[FormField]:
    """Custom-question FormFields found on a Lever apply page (empty when there are none)."""
    p = _Page()
    p.feed(html)
    p.close()
    fields = _from_json(p.templates) if p.templates else []
    if fields:
        present = {i["name"] for it in p.items for i in it["inputs"]}
        return [f for f in fields if not present or f.key in present]   # JSON for kinds, DOM only gates existence
    return _from_dom(p.items)
