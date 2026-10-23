# ADR-0050: Messaging opt-ins fail closed; flat answers never answer foreign authorization

Status: accepted · Date: 2026-10-22 · Amends ADR-0030, ADR-0038, ADR-0040 (home-country default) · Issue #70

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
1. **Contact-permission questions are opt-ins (deny by default), before every answer source.** On a choice or
   boolean field, a label that names a channel or contact word (text/SMS/MMS, call/phone, email, WhatsApp, Signal,
   Telegram, WeChat, LINE, Viber, iMessage/RCS, push, marketing/newsletter/promotions/future roles/talent community/
   job alerts, or generic contact/reach/message/updates/alerts/notifications) and addresses the candidate ("you",
   "your", "me", opt in, consent, agree, receive, OK, allow, ...) is handled by `rule:messaging_optin` only. A
   phrasing list is not needed: any such label qualifies unless it is a skills/experience question ("experience
   with Twilio", "built SMS gateways"), a question about the past or other people ("have you been contacted",
   references, employer, emergency contact), on-call/phone-screen/availability wording, an authorization or EEO
   question, or a field with its own canonical rule (referral, prior employee, contact details, ...).
   - **Yes** only when the owner stored Yes for every named channel under the exact `[answers]` key
     `optin:<channel>`: `whatsapp`, `sms`, `phone`, `email`, `signal`, `telegram`, `wechat`, `line`, `viber`,
     `imessage`, `push`, `marketing`, or `contact` (no channel named). Stored/persona answers and flat patterns
     never count.
   - Otherwise **No** when required (the single "No ..." / "do not want" option, or `false` for a checkbox), and
     **left empty** when optional (`omitted`: never a task, never a model).
   - Opt-out wording, unclear options, or an opt-in bundled with a notice, terms, attestation or recording consent
     stay unresolved for the owner. On a free-text field only explicit opt-in wording (opt in, consent, subscribe,
     "may we contact ...") is treated as an opt-in (unresolved); "WhatsApp number" or "Your email" is not.
2. **"opt-in" is one token** in `normalize_question` (`optin` / `optout`), so a flat `opt` pattern never matches it.
   Flat `[answers]` patterns already match whole words only (`\b<pattern>\b` on the normalized question); "opt" vs
   "opt in" was the one cross-token match. The OPT work-program term in the authorization-topic check likewise
   excludes "opt (me|us) in/into/out".
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
   `requires_sponsorship` fact. For a US-home profile that is `[authorization] requires_sponsorship = true|false` with
   home country US (set explicitly or derived, see 6), or `[authorization.US] requires_sponsorship = true|false`.
6. **No US default for the home country.** `[authorization].home_country` (an ISO-2 code or a country name) wins. If it
   is unset, the home country is derived from `[contact].country` via the place tables (`country_iso`: "Canada",
   "CA", "United States", "USA" ...). If neither gives a confirmed ISO-2, there is **no** home country: the flat
   `[authorization]` keys (`work_authorized`, `requires_sponsorship`, `citizen`) describe no country, and
   authorization/sponsorship questions stay unresolved and are listed as `profile_gaps` for the job's confirmed
   country (unconfirmed-country jobs stay `no_gap_keys`, as in ADR-0040). `[authorization.<CC>]` tables still answer
   for their own country. This replaces ADR-0040's `home_country` default of "US"; `mapper` no longer assumes "US"
   anywhere a home country is read.
7. Defence in depth: "WhatsApp" joins the engine's consent-label and the browser's consent-option words, so a client
   or a DOM-only group never ticks it.

## Consequences
A profile that stated authorization or sponsorship only as `[answers]` patterns now gets these questions left to the
owner. The fix is to set the structured `[authorization]` keys. A profile with flat `[authorization]` keys and neither
`home_country` nor a recognised `[contact].country` now gets those questions as profile gaps. Questions that name another country, or a job whose
country is not confirmed, stay unresolved. A missing answer is recoverable; a false Yes to an opt-in or to a
sponsorship question is not.
