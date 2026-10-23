# ADR-0050: Messaging opt-ins fail closed; flat answers never answer foreign authorization

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
4. **Flat answers never answer authorization or sponsorship.** A field whose label or options name authorization,
   sponsorship, visas or work permits is answered only from structured facts: `[authorization]` (flat keys, for
   `home_country`) or `[authorization.<CC>]`, or a stored `authorization.<CC>.*` answer (ADR-0040/0047). A missing
   home-country fact no longer falls through to `[answers]` patterns, and `country_fact` falls back only to the
   structured flat keys (`structured:<key>`). This replaces the ADR-0030 fallback to `[answers]` patterns.
   A non-home or unconfirmed-country job stays unresolved, never a reusable gap.
5. **"Sponsor you" on a confirmed home-country job.** When the question names no country and the job's confirmed
   country is the home country, the posting company's name and "for the location(s) you selected (in your previous
   response)" are removed before the strict vocabulary check. So "Will you require <company> to sponsor you for a
   work permit now or in the future for the location(s) you selected ...?" is answered from the home-country
   `requires_sponsorship` fact. For a US-home profile that is `[authorization] home_country = "US"` (the default) with
   `requires_sponsorship = true|false`, or `[authorization.US] requires_sponsorship = true|false`.
6. Defence in depth: "WhatsApp" joins the engine's consent-label and the browser's consent-option words, so a client
   or a DOM-only group never ticks it.

## Consequences
A profile that stated authorization or sponsorship only as `[answers]` patterns now gets these questions left to the
owner. The fix is to set the structured `[authorization]` keys. Questions that name another country, or a job whose
country is not confirmed, stay unresolved. A missing answer is recoverable; a false Yes to an opt-in or to a
sponsorship question is not.
