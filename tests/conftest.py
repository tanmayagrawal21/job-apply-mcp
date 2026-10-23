import os
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("JAM_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test; set JAM_LIVE=1")
    for it in items:
        if "live" in it.keywords:
            it.add_marker(skip)


@pytest.fixture
async def http(tmp_path):
    from job_apply_mcp.platforms import HttpClient
    c = HttpClient(cache_dir=tmp_path / "cache", ttl_s=900, retries=2, backoff_s=0)
    yield c
    await c.aclose()


_REAL_DATA_DIR: Path | None = None
_REAL_CONFIG_DIR: Path | None = None
_VIOLATIONS: list[str] = []
_REAL_LISTING: list[str] = []
_TOUCHED: set[str] = set()  # top-level names under the real data dir that THIS process referenced in any audited call


def _listing(root: Path | None) -> list[str]:
    """Top-level entry names only (no contents, no mtimes: other processes may legitimately use the dir)."""
    try:
        return sorted(p.name for p in root.iterdir()) if root is not None else []
    except OSError:
        return []
_ENV_KEYS = ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA")


def _is_under(path, root: Path | None) -> bool:
    try:
        p = Path(os.fsdecode(path)).expanduser().absolute()
    except (TypeError, ValueError):
        return False
    return root is not None and (p == root or root in p.parents)


def _own_new_entries(baseline: list[str], current: list[str], touched: set[str]) -> list[str]:
    """Entries that appeared since `baseline` AND that this process referenced. Entries a foreign process
    (a live dev server, another agent's dry run) created are ignored (#64)."""
    return sorted((set(current) - set(baseline)) & touched)


def _note_touch(path) -> None:
    try:
        p = Path(os.fsdecode(path)).expanduser().absolute()
    except (TypeError, ValueError):
        return
    if _REAL_DATA_DIR is not None and _REAL_DATA_DIR in p.parents:
        _TOUCHED.add(p.relative_to(_REAL_DATA_DIR).parts[0])


def _audit(event, args):
    """Record and BLOCK (raise before the filesystem call) any write-ish access to the real data dir by THIS process.
    Process-local on purpose: another process (e.g. a live server) may legitimately use that dir meanwhile."""
    if args and isinstance(args[0], (str, bytes, os.PathLike)) and (
            event == "open" or event.startswith(("os.", "shutil.", "glob."))):
        _note_touch(args[0])
    if event == "open":
        path, mode, flags = args
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND))
        if writing and _is_under(path, _REAL_DATA_DIR):
            _VIOLATIONS.append(f"open({os.fsdecode(path)!r}, {mode!r})")
            raise PermissionError(f"test suite may not write to the real data dir: {os.fsdecode(path)}")
    elif event in ("os.mkdir", "os.rename", "os.remove", "os.rmdir", "shutil.rmtree", "os.truncate"):
        if _is_under(args[0], _REAL_DATA_DIR):
            _VIOLATIONS.append(f"{event}({os.fsdecode(args[0])!r})")
            raise PermissionError(f"test suite may not modify the real data dir: {os.fsdecode(args[0])}")


def pytest_configure(config):
    """Runs before any project module is imported by test collection: remember the real dirs (for the
    guard), then point the process env at a throwaway HOME so import-time config reads are isolated too."""
    global _REAL_DATA_DIR, _REAL_CONFIG_DIR
    import atexit
    import shutil
    import sys
    import tempfile

    from job_apply_mcp import paths
    _REAL_DATA_DIR, _REAL_CONFIG_DIR = paths.data_dir().absolute(), paths.config_dir().absolute()
    sys.addaudithook(_audit)
    global _REAL_LISTING
    _REAL_LISTING = _listing(_REAL_DATA_DIR)
    boot = Path(tempfile.mkdtemp(prefix="jam-test-home-"))
    atexit.register(shutil.rmtree, boot, ignore_errors=True)
    for k in list(os.environ):
        if k.startswith("JOB_APPLY__") or k == "JOB_APPLY_MCP_CONFIG":
            del os.environ[k]
    paths._real_home = lambda: boot      # the trusted-config / state locations come from the account home, not $HOME
    os.environ.update(HOME=str(boot), USERPROFILE=str(boot), XDG_CONFIG_HOME=str(boot / ".config"),
                      XDG_DATA_HOME=str(boot / ".local" / "share"), APPDATA=str(boot / "AppData" / "Roaming"),
                      LOCALAPPDATA=str(boot / "AppData" / "Local"))


@pytest.fixture(scope="session", autouse=True)
def _real_data_dir_untouched():
    """Fail the session if this process wrote to the developer's real data dir."""
    yield
    own = _own_new_entries(_REAL_LISTING, _listing(_REAL_DATA_DIR), _TOUCHED)
    assert not own, f"this process created entries in the real data dir {_REAL_DATA_DIR}: {own}"
    assert not _VIOLATIONS, f"test suite touched the real data dir {_REAL_DATA_DIR}: {_VIOLATIONS[:5]}"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """The suite never reads the developer's real config, env overrides or data (HOME, XDG_*, APPDATA,
    LOCALAPPDATA, USERPROFILE, JOB_APPLY__*, JOB_APPLY_MCP_CONFIG all point at / are cleared for tmp_path)."""
    home = tmp_path / "home"
    home.mkdir()
    for k in list(os.environ):
        if k.startswith("JOB_APPLY__") or k == "JOB_APPLY_MCP_CONFIG":
            monkeypatch.delenv(k)
    for k, v in {"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home / ".config",
                 "XDG_DATA_HOME": home / ".local" / "share", "APPDATA": home / "AppData" / "Roaming",
                 "LOCALAPPDATA": home / "AppData" / "Local"}.items():
        monkeypatch.setenv(k, str(v))
    from job_apply_mcp import paths as _paths
    monkeypatch.setattr(_paths, "_real_home", lambda: home)
    monkeypatch.chdir(tmp_path)  # a stray ./job-apply-mcp.toml in the cwd must not leak in either
    yield home


@pytest.fixture(autouse=True)
def _isolated_data_dir(_isolated_env, tmp_path, monkeypatch):
    """No test may touch the real ~/.local/share/job-apply-mcp (audit log, store, profile, evidence):
    every Settings() built during a test points its data and browser-profile dirs at tmp_path."""
    from job_apply_mcp import paths
    from job_apply_mcp.config import Settings
    orig = Settings.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        default_data = paths.default_data_dir_str()
        if self.paths.data_dir.startswith("~") or self.paths.data_dir == default_data:
            self.paths.data_dir = str(tmp_path / "data")
        if self.paths.state_dir is None:
            self.paths.state_dir = str(tmp_path / "data")
        if self.browser.user_data_dir.startswith("~") or self.browser.user_data_dir == default_data + "/browser-profile":
            self.browser.user_data_dir = str(tmp_path / "profile")

    monkeypatch.setattr(Settings, "__init__", init)
