# ADR-0049: Messaging opt-ins fail closed; flat answers never answer foreign authorization

Status: accepted · Date: 2026-10-22 · Amends ADR-0030, ADR-0038, ADR-0040 · Issue #70

## Context
QA (#70) found two wrong answers on Greenhouse postings:

1. "Do you opt-in to receive WhatsApp messages from <company> Recruiting?" was answered **Yes**. No rule knew
   WhatsApp, so the label fell through to the flat `[answers]` lookup. `normalize_question` turns "opt-in" into
   "opt in", and a flat pattern `opt` (the US work program, answered Yes) matched it as a whole word.
2. On a Canadian job, "Will you require <company> to sponsor you for a work permit now or in the future ...?" was
   answered from a flat pattern. The `requires_sponsorship` alias only knew "sponsorship" and "require ... visa",
   so "sponsor you" had no canonical key, skipped the per-country rule (ADR-0040) and matched the flat pattern
   "now or in the future".

## Decision
1. **Messaging/marketing opt-ins, before every answer source.** A choice field whose label names a channel
   (WhatsApp, SMS/MMS/text messages, phone/mobile messaging, Telegram/WeChat/Viber/iMessage/RCS, push
   notifications, marketing/newsletter/promotions) and opt-in wording is handled by `rule:messaging_optin` only:
   - **Yes** only when the owner stored Yes for every named channel under the exact `[answers]` key
     `optin:<channel>` (`whatsapp`, `sms`, `phone`, `marketing`). Stored/persona answers and flat patterns never count.
   - Otherwise **No** when required (the single "No ..." / "do not want" option, or `false` for a checkbox), and
     **left empty** when optional (`omitted`: never a task, never a model).
   - Opt-out wording, unclear options, or an opt-in bundled with a notice, terms, attestation or recording consent
     stay unresolved for the owner. A text field that only names a channel ("WhatsApp number") is not an opt-in.
2. **"opt-in" is one token** in `normalize_question` (`optin` / `optout`), so a flat `opt` pattern never matches it.
3. **Any form of "sponsor"** maps to `requires_sponsorship`, so such questions take the per-country rule.
4. **Flat answers are home-country facts.** In the generic lookup, a field whose label or options name
   authorization, sponsorship, visas or work permits is answered only when its country (the one it names, else the
   job's confirmed country) is the home country. Otherwise it is unresolved and never a reusable gap. A non-home job
   uses only `[authorization.<CC>]` facts or a stored `authorization.<CC>.*` answer (ADR-0040).
5. Defence in depth: "WhatsApp" joins the engine's consent-label and the browser's consent-option words, so a client
   or a DOM-only group never ticks it.

## Consequences
Some questions answered before are now left to the owner (for example a sponsorship question that also names the
company or "the location(s) you selected"). That is deliberate: a missing answer is recoverable, a false Yes to an
opt-in or a sponsorship question is not.
