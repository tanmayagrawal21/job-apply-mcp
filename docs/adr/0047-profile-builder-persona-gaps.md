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
3. **Ask when present.** MCP tools `profile_questions(limit)` and `answer_profile_question(key, value, scope,
   scope_value)` (scopes `global | company | role_family`; origin always `user_written`). `plan_application` and
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

7. **Trust boundary (amendment after review).** `answer_profile_question` asks the human through MCP elicitation
   (confirm or edit); only a confirmed value, or one typed in the TTY CLI, is stored `user_written`. Any other call
   stores `origin=client`; in `approval_mode=human` it is refused. For sensitive keys (authorization, sponsorship,
   citizenship, clearance, export, privacy acknowledgement, salary floor; country-agnostic, so `key:DE` style and
   per-country maps match) a client answer is only a suggestion shown to the human and is never applied.
   Demographic keys come from `[demographics]` only. Blocked labels (attestation, certification, consent, arbitration,
   criminal, AI-use, clearance/export, EEO, stop phrases, `is_sensitive_question`) are never collected, asked, stored
   or claimed. The mapper hook runs after all sensitive guards, only for non-canonical fields, and persona patterns
   must match the whole question. `ProfileView.lookup` uses stored answers for canonical keys only. Elicitation is
   once per client session.

## Consequences
- Safe defaults remain (decline, full-time only, midpoint); every default use becomes a ranked, askable question.
- A second SQLite file (`profile.db`); no new dependency.
- Notes in a string-valued policy are advisory only; behaviour changes need the structured keys or an answer.

## Verify / revert
`uv run pytest tests/test_persona.py -q`. Revert: `git revert` the squash commit, or set `persona.enabled = false`
to stop recording and applying (the `[persona]` section is then parsed but ignored, `profile.db` is left untouched).
