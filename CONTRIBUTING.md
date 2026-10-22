# Contributing and decision process

Every non-trivial decision must be **independently verifiable and reversible**.

1. **One decision per branch and PR.** Branch names: `feat/<component>`, `fix/<topic>`, `adr/<topic>`,
   `chore/<topic>`.
2. **Decision record:** a PR that makes or changes a design decision adds or updates
   `docs/adr/NNNN-*.md` with Context, Decision, Consequences and **Verify / revert** (the exact test
   command and what to revert).
3. **Tests:** every component ships tests runnable offline (HTTP mocked with `respx`, LLMs with a fake
   provider). CI must be green.
4. **Merge:** squash-merge only, so each PR is one revertable commit on `main` (`git revert <sha>`).
5. **No personal data:** profiles, resumes, answers and API keys never enter the repo. Examples live in
   `examples/` with fictional values.
6. **Dependencies:** follow ADR-0004. New dependencies need a sentence of justification in the PR.
