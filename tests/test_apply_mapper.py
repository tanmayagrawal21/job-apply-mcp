from apply_fakes import F, FakeProfile

from job_apply_mcp.apply.mapper import best_option, canonical_key, map_fields, normalize
from job_apply_mcp.core.models import FieldKind


def test_normalize_and_alias():
    assert normalize("First Name *") == "first name"
    assert normalize("Email (optional)") == "email"
    assert canonical_key("Legal First Name") == "first_name"
    assert canonical_key("Will you now or in the future require sponsorship?") == "requires_sponsorship"
    assert canonical_key("Favorite color") is None


def test_best_option_variants():
    opts = ["Male", "Female", "I don't wish to answer"]
    assert best_option("decline", opts) == "I don't wish to answer"
    assert best_option("Decline to self-identify", ["Yes", "Prefer not to say"]) == "Prefer not to say"
    assert best_option("yes", ["Yes, I am", "No"]) == "Yes, I am"
    assert best_option("no", ["Yes, I am", "No, I am not"]) == "No, I am not"
    assert best_option("Masters Degree", ["Bachelor's Degree", "Master's Degree", "MBA"]) == "Master's Degree"
    assert best_option("zzz", opts) is None
    assert best_option("x", []) is None


def test_map_fields_files_selects_unresolved():
    fields = [F("first_name", "First Name"), F("resume", "Resume/CV", FieldKind.file),
              F("cl", "Cover Letter", FieldKind.file, required=False),
              F("g", "Gender", FieldKind.select, options=["Man", "Woman", "Prefer not to say"]),
              F("x", "Favorite build tool")]
    res = map_fields(fields, FakeProfile())
    got = {a.key: a.value for a in res.answers}
    assert got == {"first_name": "Ada", "resume": "/tmp/resume.pdf", "g": "Prefer not to say"}
    assert [f.key for f in res.unresolved] == ["x"]


def test_max_length_truncates_text():
    class P(FakeProfile):
        def lookup(self, key):
            return "a" * 50 if key == "first_name" else None
    res = map_fields([F("first_name", "First Name", max_length=10)], P())
    assert res.answers[0].value == "a" * 10
