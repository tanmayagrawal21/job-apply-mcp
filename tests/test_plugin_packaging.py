"""Claude Code plugin, MCP Registry and MCPB packaging (ADR-0042): safe defaults and consistent, pinned versions."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from job_apply_mcp.config import Settings, load_settings
from job_apply_mcp.control.builtin import effective_limits

ROOT = Path(__file__).resolve().parents[1]
PYPI_PIN = re.compile(r"^applywise@\d+\.\d+\.\d+$")


def _json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _repo_url() -> str:
    return _pyproject()["project"]["urls"]["Repository"]          # https://github.com/<owner>/<repo>


def _git_pin() -> re.Pattern:
    return re.compile(rf"^git\+{re.escape(_repo_url())}@([0-9a-f]{{40}}|v\d+\.\d+\.\d+)$")


# --- safe defaults on a fresh install (conftest isolates HOME/XDG/env, so no user config exists) -------------


def test_fresh_install_has_safe_defaults():
    from job_apply_mcp import paths

    assert not paths.config_file().exists()
    for s in (load_settings(), Settings()):
        assert s.dry_run is True
        assert s.apply.approval_mode == "human"
        assert s.apply.auto_approve is False
        assert s.browser.captcha_policy == "handoff"
        assert s.writing.reviewer == "client"
        assert s.control.apply_enabled is True and "quota" in s.control.enabled
        limits = effective_limits(s.control.params.get("quota", {}), s)
        assert all(isinstance(v, int) and v > 0 for v in limits.values()), limits
        assert (s.apply.daily_limit, s.apply.daily_limit_per_platform, s.apply.run_limit) == (25, 10, 10)


# --- plugin + marketplace -------------------------------------------------------------------------------------


def test_plugin_manifest_matches_package():
    plugin = _json(".claude-plugin/plugin.json")
    assert plugin["name"] == "applywise"
    assert plugin["version"] == _pyproject()["project"]["version"]
    assert set(plugin["author"]) == {"name"}          # handle only, no email
    assert plugin["license"] == _pyproject()["project"]["license"]
    for k in ("description", "homepage", "repository", "keywords"):
        assert plugin[k]


def test_marketplace_lists_the_plugin_from_repo_root():
    mkt = _json(".claude-plugin/marketplace.json")
    assert mkt["name"] == "applywise" and mkt["owner"]["name"]
    assert [(p["name"], p["source"]) for p in mkt["plugins"]] == [("applywise", "./")]
    assert "version" not in mkt["plugins"][0]          # plugin.json is the single version source


def _server_args() -> list[str]:
    servers = _json(".mcp.json")["mcpServers"]
    assert list(servers) == ["applywise"]
    srv = servers["applywise"]
    assert srv["type"] == "stdio" and srv["command"] == "uvx"
    return srv["args"]


def test_mcp_json_is_pinned():
    args = _server_args()
    if args[0] == "--from":                            # git fallback: commit SHA or release tag, never a branch
        assert _git_pin().match(args[1]), args[1]
        assert args[2] in ("job-apply-mcp", "applywise")
    else:                                              # PyPI form after the first release
        assert len(args) == 1 and PYPI_PIN.match(args[0]), args


def test_skill_uses_the_same_pin_and_stays_generic():
    text = (ROOT / "skills/applywise/SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: applywise\ndescription: ")
    args = _server_args()
    pin = args[1] if args[0] == "--from" else args[0]
    assert pin in text, "SKILL.md init/doctor commands must use the .mcp.json pin"
    for leak in ("/Users/", "/home/", "C:\\Users", "@gmail", "@arizona"):
        assert leak not in text
    for word in ("captcha", "password", "grant", "approval_mode", "dry_run", "daily"):
        assert word in text.lower()


# --- PyPI / MCP Registry / MCPB -------------------------------------------------------------------------------


def test_pypi_distribution_and_scripts():
    proj = _pyproject()["project"]
    assert proj["name"] == "applywise"
    assert proj["license"] == "MIT"
    assert proj["scripts"]["applywise"] == proj["scripts"]["job-apply-mcp"] == "job_apply_mcp.cli:main"
    for k in ("readme", "requires-python", "classifiers", "urls", "description"):
        assert proj[k]


def test_registry_server_json_matches_package_and_readme():
    srv = _json("server.json")
    version = _pyproject()["project"]["version"]
    owner = _repo_url().removeprefix("https://github.com/").split("/")[0]
    assert srv["name"] == f"io.github.{owner}/applywise"
    assert srv["repository"]["url"] == _repo_url()
    assert srv["version"] == version
    assert len(srv["description"]) <= 100
    (pkg,) = srv["packages"]
    assert (pkg["registryType"], pkg["identifier"], pkg["version"]) == ("pypi", "applywise", version)
    assert pkg["transport"] == {"type": "stdio"}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"<!-- mcp-name: {srv['name']} -->" in readme


def test_mcpb_bundle_matches_release():
    man = _json("mcpb/manifest.json")
    version = _pyproject()["project"]["version"]
    assert man["manifest_version"] == "0.4" and man["server"]["type"] == "uv"
    assert man["name"] == "applywise" and man["version"] == version
    assert (ROOT / "mcpb" / man["server"]["entry_point"]).is_file()
    deps = tomllib.loads((ROOT / "mcpb/pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert deps == [f"applywise=={version}"]
