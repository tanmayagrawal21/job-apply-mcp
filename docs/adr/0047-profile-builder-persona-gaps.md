# ADR-0047: Profile builder: persona policies, gap collector, stored answers

## Context
The tool answers what the profile states and guesses or declines the rest. Whatever it could not answer was
visible only in one plan's output, so the same gaps came back on every application, and judgement calls (what counts
as professional experience, what salary to enter, which project to showcase) were hard-coded or left to a model.
The tool must work for any user, so none of this may encode one person's choices.

## Decision
1. **`[persona]` profile section** (`persona/models.py`). `experience_counting` (mode `full_time_only` default,
   `include_internships`, `all`; `count_from`; `include_orgs`/`exclude_orgs`), `salary_policy` (`midpoint` default,
   `floor`, `top`; `floor`; `fixed` for postings without a range), `exceptional_work` (several `projects` with
   keywords, plus the resume's project facts), and reusable `answers` (AI tools used, etc.). Each policy also accepts
   a bare string, kept verbatim as `note`; notes are never parsed, so existing keys load unchanged and nothing is
   rewritten. `Work.kind` is optional; when empty the role type is inferred from the title (intern, part-time,
   teaching/research assistant, volunteer, freelance), else full-time.
2. **Gap collector** (`persona/collector.py`, `persona/gaps.py`, `profile.db` in the data dir). After each plan,
   every unresolved field and every default-used answer (declined demographics, model guesses, default experience
   counting, default salary) is stored per (question key, application ref): kind, blocked, default used, company,
   title. Keys are the mapper's canonical keys, `ai_tools`, `persona.*`, or `q:<normalized question>`. Re-planning a
   ref replaces its rows. Ranking = frequency x impact: a hit weighs 3 when a required field blocked the
   application, 2 for an open optional field, 1 for a default; a key's score is the sum.
3. **Ask when present.** MCP tools `profile_questions(limit)` and `answer_profile_question(key, scope, scope_value)` (elicits the value from the user; scopes `global | company | role_family`; origin always `user_written`). `plan_application` and
   `apply_batch` call `ask_at_start` before any planning: one MCP-elicitation form per server session with the top
   `persona.ask_limit` gaps, silent when the client cannot elicit. Blank answers mark the gap `skipped`. CLI:
   `job-apply-mcp profile ask|list|answer` (`ask` needs a TTY).
4. **Enforcement through a small interface** (`PersonaPolicy`: `claim`, `stored`, `counted_spans`, `salary`,
   `exceptional`). Mapper: one `persona=` hook that claims free-form (non-canonical) questions from stored/persona
   answers. Experience years: `ProfileView._experience_spans` counts only qualifying roles. Salary: the engine calls
   `persona.salary`, which wraps `compute_ask` (midpoint, never below the floor, never above the top); with no range
   never below the floor unless the posted top is lower (then the top); a profile floor can only raise `policy.salary_floor`; with no range it uses `fixed`, then `[salary].expected`, then `policy.default_ask`, else the gap is recorded. Writer:
   `ProfileView.facts` leads with the best-matching project for exceptional-work questions. The current posting
   reaches the policy through a ContextVar, so parallel plans stay isolated.
5. **Stored answers fill gaps, never override.** `ProfileView.lookup` consults stored answers only when the profile has
   no value (or the declining default). Precedence: company, role family, global. Answers to `persona.*` questions
   overlay the policy (`persona.experience_counting`, `persona.salary_no_range`, `persona.exceptional_work`).
6. Config: `[persona] enabled, ask_on_start, ask_limit, min_hits`.

7. **Trust boundary (final, after three review rounds).** Only the human writes answers. Answers are stored
   `user_written` through exactly two paths: the elicitation form (start-of-session round, or
   `answer_profile_question`) and the TTY CLI. `answer_profile_question(key, scope, scope_value)` takes **no value**
   from the client: it only starts an elicitation and stores nothing if the client cannot elicit. `ProfileDB`
   refuses any origin other than `user_written`. Demographic keys are never stored or collected ([demographics] only).
8. **Fail-closed hook.** `persona.claim` (called after all mapper sensitive guards, only for non-canonical fields)
   is a positive allowlist: `ai_tools`, and `q:` questions whose label is a plain fact about tools, editors,
   frameworks, notice period, start date or links (`guard.plain_fact_label`), and which also clear `blocked_label`
   (attestation, certification, consent, arbitration, criminal, AI-use, clearance/export, relocation, EEO, stop
   phrases). Persona patterns must match the whole question. Canonical keys reach stored answers only through
   `ProfileView.lookup` (never for demographics). The collector records, and the CLI/elicitation asks, only what an
   answer could actually fill. The sensitive-key matcher is country-agnostic (`work_authorized:DE`, per-country maps).
   Policies read the three `persona.*` answers (experience counting, no-range salary figure, preferred project).

## Consequences
- Safe defaults remain (decline, full-time only, midpoint); every default use becomes a ranked, askable question.
- A second SQLite file (`profile.db`); no new dependency.
- Notes in a string-valued policy are advisory only; behaviour changes need the structured keys or an answer.

## Verify / revert
`uv run pytest tests/test_persona.py -q`. Revert: `git revert` the squash commit, or set `persona.enabled = false`
to stop recording and applying (the `[persona]` section is then parsed but ignored, `profile.db` is left untouched).
