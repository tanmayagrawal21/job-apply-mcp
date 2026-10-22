import difflib

from job_apply_mcp.config import Settings
from job_apply_mcp.tuning import TuneManager, build_dataset
from job_apply_mcp.tuning.dataset import reverse_unified_diff
from job_apply_mcp.tuning.evalgate import lint_violations
from job_apply_mcp.tuning.source import ProvenanceRecord, trusted_items
from job_apply_mcp.tuning.trainer import TrainResult


def rec(i, text, origin="user_written", kind="cover_letter", **kw):
    return ProvenanceRecord(id=i, kind=kind, text=text, origin=origin, context=f"role {i}", **kw)


def body(n, seed):
    return " ".join(f"{seed}{k % 7}word" for k in range(n))


class Src:
    def __init__(self, recs):
        self.recs = recs

    def items(self, kinds=None):
        return [r for r in self.recs if not kinds or r.kind in kinds]


class FakeTrainer:
    name = "fake"

    def __init__(self, good=True):
        self.good, self.trained = good, []

    def available(self):
        return True, "ok"

    def train(self, spec):
        spec.adapter_dir.mkdir(parents=True, exist_ok=True)
        (spec.adapter_dir / "adapters.safetensors").write_text("x")
        self.trained.append(spec)
        return TrainResult(spec.adapter_dir, 0.1, spec.cfg.iters)

    def generate(self, base, adapter, prompts, max_tokens):
        return ["I'm thrilled to apply."] * len(prompts) if adapter is None else [body(30, "a")] * len(prompts)

    def nll(self, base, adapter, pairs):
        if adapter is None:
            return 2.0
        if not self.good:
            return 2.5
        return 1.5 if adapter == self.trained[0].adapter_dir else 1.0     # later runs improve


def settings(tmp_path, **tune):
    return Settings.model_validate({"paths": {"data_dir": str(tmp_path)}, "tune": {"eval_fraction": 0.5, **tune}})


def many(n=6, extra=()):
    return Src([rec(f"r{i}", body(40, chr(97 + i))) for i in range(n)] + list(extra))


def test_llm_generated_never_enters(tmp_path):
    src = many(4, [rec("llm", body(40, "z"), origin="llm_generated")])
    assert "llm" not in {r.id for r in trusted_items(src)}
    ds = build_dataset(src, settings(tmp_path).tune, tmp_path / "d")
    assert "llm" not in ds.source_ids
    assert "zword" not in (tmp_path / "d" / "train.jsonl").read_text()


def test_dataset_dedupe_filters_split_and_facts(tmp_path):
    same = body(40, "q")
    src = Src([rec("a", same), rec("b", same), rec("c", "too short"), rec("d", body(40, "d")), rec("e", body(40, "e")),
               rec("f", "I live in Tucson and build things.", kind="profile_fact")])
    ds = build_dataset(src, settings(tmp_path, eval_fraction=0.34).tune, tmp_path / "d")
    assert ds.dropped == {"duplicate": 1, "length": 1}
    assert len(ds.train) + len(ds.eval) == 3 and len(ds.eval) == 1 and len(ds.train) >= 1
    assert "Tucson" in ds.train[0]["messages"][1]["content"]
    assert (tmp_path / "d" / "valid.jsonl").read_text().count("\n") == 1
    ds2 = build_dataset(src, settings(tmp_path, eval_fraction=0.34).tune, tmp_path / "d2")
    assert ds2.eval == ds.eval        # deterministic


def test_preference_pairs_from_user_edited(tmp_path):
    draft = "I am thrilled to apply.\nI built a tool.\nThanks."
    edited = "I am applying.\nI built a tool.\nThanks."
    diff = "\n".join(difflib.unified_diff(draft.splitlines(), edited.splitlines(), "p", "c", lineterm=""))
    assert reverse_unified_diff(edited, diff) == draft
    src = many(3, [rec("e1", edited + " " + body(30, "k"), origin="user_edited", diff_from_parent=None)])
    ds = build_dataset(src, settings(tmp_path, min_words=3).tune, tmp_path / "d")
    assert ds.prefs == []     # no diff -> no pair
    full = edited + " " + body(30, "k")
    d2 = "\n".join(difflib.unified_diff((draft + " " + body(30, "k")).splitlines(), full.splitlines(), lineterm=""))
    src = many(3, [rec("e1", full, origin="user_edited", diff_from_parent=d2)])
    ds = build_dataset(src, settings(tmp_path, min_words=3).tune, tmp_path / "d3")
    assert len(ds.prefs) == 1 and "thrilled" in ds.prefs[0]["rejected"] and "thrilled" not in ds.prefs[0]["chosen"]


def test_gate_promotes_then_rolls_back(tmp_path):
    m = TuneManager(settings(tmp_path), many(), FakeTrainer(), battery=lambda: False, resident=lambda s: [])
    r1 = m.run("manual")
    assert r1["status"] == "promote", r1
    assert m.status()["promoted"] == r1["run"] and m.status()["new_texts"] == 0
    # second run continues on top of the promoted adapter
    t = m.trainer
    import time
    time.sleep(1.1)
    r2 = m.run("manual")
    assert t.trained[-1].resume_adapter == m.adapter_dir(r1["run"])
    assert m.status()["previous"] == r1["run"] and m.status()["promoted"] == r2["run"]
    assert m.rollback()["promoted"] == r1["run"]
    assert m.status()["promoted"] == r1["run"]


def test_gate_rejects_worse_adapter(tmp_path):
    m = TuneManager(settings(tmp_path), many(), FakeTrainer(good=False), battery=lambda: False, resident=lambda s: [])
    r = m.run("manual")
    assert r["status"] == "reject" and any("loss" in x for x in r["gate"]["reasons"])
    assert m.status()["promoted"] is None


def test_gate_rejects_when_too_little_eval(tmp_path):
    src = Src([rec("a", body(40, "a"))])
    r = TuneManager(settings(tmp_path), src, FakeTrainer(), battery=lambda: False, resident=lambda s: []).run()
    assert r["status"] == "reject" and "cannot gate" in r["gate"]["reasons"][0]


def test_guards_and_new_data_threshold(tmp_path):
    m = TuneManager(settings(tmp_path), many(), FakeTrainer(), battery=lambda: True, resident=lambda s: [])
    assert "battery" in m.run("manual")["reason"]
    m = TuneManager(settings(tmp_path), many(), FakeTrainer(), battery=lambda: False, resident=lambda s: ["gemma"])
    assert "resident" in m.run("manual")["reason"]
    m = TuneManager(settings(tmp_path, allow_on_battery=True, allow_with_heavy_model=True), many(), FakeTrainer(),
                    battery=lambda: True, resident=lambda s: ["gemma"])
    assert m.run("new_data")["status"] == "skipped"          # 6 < threshold 10
    m = TuneManager(settings(tmp_path, new_data_threshold=3), many(), FakeTrainer(), battery=lambda: False,
                    resident=lambda s: [])
    assert m.run("new_data")["status"] == "promote"


def test_lint():
    assert lint_violations("I'm thrilled.") >= 2
    assert lint_violations("I wrote the planner.") == 0


def test_tools_registered():
    from job_apply_mcp import server
    names = {t.name for t in __import__("asyncio").run(server.mcp.list_tools())}
    assert {"tune_status", "tune_run"} <= names
