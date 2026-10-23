# ADR-0057: Live-run single-field blockers (residence region, explicit-country authorization, status options)

Status: accepted · Date: 2026-10-23 · Amends ADR-0040, ADR-0053, ADR-0055

## Context
A live run was blocked by single fields the profile can answer. These were a state/province question, authorization
wordings that name the country or the company, an "are you currently an employee of a <company> agency" question, a
sponsorship select whose options are legal statuses, and a bare "Visa" checkbox.

## Decision
1. **Residence region.** "Which state, province, or region do you currently live in?" (`residence_region`, a
   `PROFILE_FACT_KEY`) is answered from `[contact].region`. A US code becomes the full state name for text and
   location fields. A select takes the option equal to the region's name or code. A missing region is a profile gap.
2. **Job country.** "Hybrid", "on site" and "in office" are noise words around a hub city ("Hybrid - Denver").
3. **Explicit country (#54).** When the question names the US as the place of work ("authorized to work lawfully in
   the United States for <company>"), the company name is removed and the US facts answer it even if the job
   country is unconfirmed. A confirmed *other* job country still blocks, and the US named only as a visa's country
   ("a US work visa") still needs a confirmed US job.
4. **Sponsorship wording.**
   - "require <company> to commence ('sponsor') an immigration case in order to employ you" reads as "sponsor".
   - Example clauses may contain generic words ("other employment-based immigration case").
   - A note after the question may say "this is sometimes called sponsorship".
   - "Our company" is generic wording.
   - "The country of origin for this position" is the confirmed job country.
5. **"Do you REQUIRE work authorization ...?"** asks whether the owner lacks authorization. The answer is Yes when
   the owner is not authorized, and No when authorized and no sponsorship is needed. Authorized now but sponsorship
   needed later stays with the owner.
6. **Status options.** A plain sponsorship question whose options are legal statuses ("Citizen", "Permanent
   Resident", "Visa Holder", "Require Sponsorship") may take only the sponsorship option, and only when
   `requires_sponsorship = true`. Every other case is unchanged (ADR-0040 legal-status block).
7. **Current employee.** "Are you currently an employee of <company> (or a <company> agency/affiliate)?" is about
   the current `[[work]]` employer only. The answer is Yes when it is that company and No otherwise, including when
   the owner has no current entry. Several current entries make it ambiguous (unresolved). An affiliate the profile
   does not name counts as "No": the owner asked for this, and the current employer is a profile fact.
8. **Bare "Visa".** A label that is only "Visa" / "Visa status" is a sponsorship question, and only on a confirmed
   home-country job.

## Consequences
More live postings reach `dry_run_ok`. Every answer is still a structured profile fact. Compound or other-country
wordings stay unresolved, as before.
