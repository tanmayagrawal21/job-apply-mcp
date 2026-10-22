from pathlib import Path

import pytest

from job_apply_mcp.config import Settings
from job_apply_mcp.profile import DECLINE, load_profile

EX = Path(__file__).parent.parent / "examples"


def test_example_profile_loads_and_answers():
    p = load_profile(EX / "profile.example.toml")
    assert p.contact.full_name == "Alex Rivera"
    assert p.graduation() == "2026-05"
    assert p.answer_for("Are you legally authorized to work in the US?") == "Yes"
    assert p.answer_for("Will you now or in the future require sponsorship?") == "No"
    assert p.answer_for("Are you 18 years of age?") == "Yes"
    assert p.answer_for("Favourite colour?") is None
    assert p.demographic_for("Veteran Status") == DECLINE
    assert p.demographic_for("What is your gender?") == DECLINE
    assert p.demographic_for("Phone number") is None


def test_missing_profile():
    with pytest.raises(FileNotFoundError):
        load_profile("/nonexistent/profile.toml")


def test_example_config_validates():
    import tomllib

    s = Settings.model_validate(tomllib.loads((EX / "config.toml").read_text()))
    assert s.policy.dedupe_days == 180 and s.dry_run is True
