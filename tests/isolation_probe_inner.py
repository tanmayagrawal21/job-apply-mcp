"""Run only by test_isolation.py in a subprocess (not collected: name does not match test_*.py)."""
from job_apply_mcp import paths
from job_apply_mcp.config import load_settings


def test_probe_ignores_hostile_environment(tmp_path):
    assert tmp_path in paths.config_dir().parents and tmp_path in paths.data_dir().parents
    assert tmp_path in paths.config_file().parents
    s = load_settings()
    assert s.dry_run is True
    assert s.apply.approval_mode != "agent"
    assert not s.apply.auto_approve
    assert tmp_path in __import__("pathlib").Path(s.paths.data_dir).parents
