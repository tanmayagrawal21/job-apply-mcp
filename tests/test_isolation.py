"""The suite must not depend on the developer's real config/env (hostile config in the real HOME)."""
import os
import subprocess
import sys
from pathlib import Path

from job_apply_mcp import paths
from job_apply_mcp.config import load_settings

HOSTILE = '[apply]\napproval_mode = "agent"\nauto_approve = true\n\n[control]\napply_enabled = true\n'


def test_fixture_redirects_paths_and_env(tmp_path):
    for p in (paths.config_dir(), paths.data_dir(), paths.config_file(), Path.home()):
        assert tmp_path in p.parents or p == tmp_path
    assert not [k for k in os.environ if k.startswith("JOB_APPLY__")]
    assert "JOB_APPLY_MCP_CONFIG" not in os.environ


def test_loader_honours_trusted_user_config_sanity(tmp_path):
    # sanity: the loader really would honour such a file, so the subprocess test below is meaningful
    cfg = paths.config_dir()
    cfg.mkdir(parents=True)
    f = cfg / "config.toml"
    f.write_text("dry_run = false\n" + HOSTILE, encoding="utf-8")
    f.chmod(0o600)
    if os.name != "nt":
        assert load_settings().dry_run is False


def test_hostile_real_home_has_no_effect_on_suite(tmp_path):
    fake_real = tmp_path / "fake_real_home"
    cdir = fake_real / ".config" / "job-apply-mcp"
    cdir.mkdir(parents=True)
    f = cdir / "config.toml"
    f.write_text("dry_run = false\n" + HOSTILE, encoding="utf-8")
    f.chmod(0o600)
    other = tmp_path / "other.toml"
    other.write_text("dry_run = false\n" + HOSTILE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "JOB_APPLY"))}
    env.update(HOME=str(fake_real), USERPROFILE=str(fake_real), APPDATA=str(fake_real / "AppData" / "Roaming"),
               LOCALAPPDATA=str(fake_real / "AppData" / "Local"), JOB_APPLY_MCP_CONFIG=str(other),
               JOB_APPLY__DRY_RUN="false", JOB_APPLY__APPLY__APPROVAL_MODE="agent",
               XDG_CONFIG_HOME=str(fake_real / ".config"), XDG_DATA_HOME=str(fake_real / ".local" / "share"))
    root = Path(__file__).parent
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        str(root / "isolation_probe_inner.py")],
                       env=env, cwd=root.parent, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-1000:]
    assert not (fake_real / ".local").exists()  # the data dir under the hostile home was never created
