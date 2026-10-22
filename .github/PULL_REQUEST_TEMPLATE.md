## What and why

## Decisions (each with how to verify and how to revert; add/update an ADR in docs/adr/)

## Checklist
- [ ] `uv run ruff check . && uv run pytest -q` pass; tests run offline
- [ ] `python scripts/privacy_scan.py --diff origin/main` is clean; no personal data in examples
- [ ] Safety invariants intact: `dry_run` defaults to true; no CAPTCHA/account/verification handling
- [ ] New dependency? Justified in one line (ADR-0004)
- [ ] Config knobs added to `config.py`; `python scripts/gen_config_docs.py` re-run
- [ ] CHANGELOG updated
