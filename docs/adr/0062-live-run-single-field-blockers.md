# ADR-0062: Live-run blockers (residence region, explicit-country authorization, job country, graduation and citizenship gates, based-or-relocate)

Status: accepted · Date: 2026-10-27 · Amends ADR-0009, ADR-0040, ADR-0053, ADR-0055

## Context
A live run was blocked by single fields the profile can answer. These were a state/province question, authorization
wordings that name the country or the company, an "are you currently an employee of a <company> agency" question, a
sponsorship select whose options are legal statuses, and a bare "Visa" checkbox.

## Decision
1. **Residence region.** "Which state, province, or region do you currently live in?" (`residence_region`, a
   `PROFILE_FACT_KEY`) is answered from `[contact].region`. A US code becomes the full state name for text and
   location fields. A select takes the option equal to the region's name or code. A missing region is a profile gap.
2. **Job country.** "Hybrid", "on site" and "in office" are noise words around a hub city ("Hybrid - Denver").
3. **Explicit country (#54).** When the question names the US *in full* as the place of work ("authorized to work
   lawfully in the United States for <company>", "in the USA"), the company name is removed and the US facts answer
   it even if the job country is unconfirmed. The short forms "US" / "U.S." keep the ADR-0053 rule (a confirmed US
   job): once normalized they are also the pronoun "us", and the ADR-0053/0055 tests pin that behaviour. A confirmed
   *other* job country still blocks, and the US named only as a visa's country ("a US work visa") still needs a
   confirmed US job.
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

9. **Job place wording.** "the country (in) which you are applying" means the posting's country, like "the country
   where the job is located" (answered only on a confirmed job country).
10. **Job country confirmation.** A `;` / `|` list whose every entry is itself a confirmed place in the structured
    country ("San Francisco, CA; Seattle, WA; New York, NY") confirms it; any region word, remote entry or other
    country still does not. A Greenhouse office confirms the US when its location is a strictly parsed US place
    ("Acme HQ - Mountain View, CA") and neither its name nor its location names another country, or when a
    location-less office is named "Remote - United States" / "United States (Remote)". Codes ("US-PERM") never do.
11. **Graduation gate.** One parser (`core/gradwindow.py`) serves the search prefilter and the gate. The candidate's
    date is the profile's latest education end date (`policy.graduation` only when the profile has none), widened by
    `policy.graduation_flex_months` on both sides. A posting is gated only on a clear mismatch: it states at least one
    window and the date fits none. Lists ("class of 2026, 2027 or 2028") are alternatives; "by June 2028", "2028 or
    earlier" and "from 2024 onwards" are open-ended; text with no parseable window never gates.
12. **Citizenship gate.** One pattern set (`core/legalflags.py`) serves the search prefilter and the gate. Every
    requirement form is flagged: "must be a U.S. citizen", "must be a citizen of the United States", "U.S.
    citizenship is a requirement", "citizenship required", "U.S. citizens only", "must be a U.S. national", and the
    permanent-resident forms ("... or green card holder", "U.S. citizens and permanent residents only", "must be a
    lawful permanent resident or U.S. citizen"), which go to the U.S.-person gate. The one exemption is "citizen OR
    <work eligibility>" ("or eligible / authorized to work", "or have work authorization"). EEO lines ("citizenship
    status") and bare export-control mentions contain no requirement form. Posting-flag snippets are sorted to their
    own gate (clearance, U.S. person, citizenship), and the search prefilter honours `policy.us_citizen`,
    `us_person` and `has_clearance` like the gate instead of dropping every flagged posting.
13. **Based in, or willing to relocate to, <place>.** Answered from `[contact]` residence and
    `[preferences].willing_to_relocate`: Yes when relocation is true or the owner lives there (confirmed city +
    region); No only when the owner is confirmed elsewhere and relocation is false; otherwise unresolved with the
    `preferences.willing_to_relocate` gap (the owner decides). A generic "this location" uses the posting locations.
14. **Owner corrections.** The owner's answer to a recorded preference gap (`profile.preferences.<field>`, given
    through `answer_profile_question` or `profile ask`) fills that unset `[preferences]` fact for the relocation,
    hybrid and on-site rules. A value set in the profile still wins.

## Consequences
More live postings reach `dry_run_ok`: US jobs listed as several cities or under an HQ office name are confirmed,
and fewer postings are skipped by boilerplate citizenship text or open-ended graduation wording. Every answer is
still a structured profile fact or the owner's own answer. Compound or other-country wordings stay unresolved, as
before. Prior-employment questions "in any capacity" / "or an affiliate" stay with the owner (ADR-0038/0053).
