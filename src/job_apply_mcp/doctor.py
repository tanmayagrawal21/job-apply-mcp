"""``job-apply-mcp doctor``: diagnose config, profile, models, browser and data directory; print fixes."""
from __future__ import annotations

import json
import os
import platform
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from pydantic import ValidationError

from . import __version__, paths
from .config import Settings, load_settings
from .profile import load_profile
from .sysinfo import find_system_chrome, total_ram_gb

OK, WARN, FAIL, INFO = "ok", "warn", "FAIL", "info"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _config_checks() -> tuple[list[Check], Settings | None]:
    cf = paths.config_file()
    out: list[Check] = []
    try:
        s = load_settings()
    except (ValidationError, tomllib.TOMLDecodeError, OSError) as e:
        msg = str(e).splitlines()
        out.append(Check("config", FAIL, f"{cf}: {msg[0] if msg else e}" + (f" ({msg[1].strip()})" if len(msg) > 1 else ""),
                         "Fix the key named above (see docs/configuration.md) or re-create with `job-apply-mcp init --force`."))
        return out, None
    out.append(Check("config", OK if cf.is_file() else WARN,
                     f"{cf} valid" if cf.is_file() else f"no config file at {cf}; using built-in defaults",
                     "" if cf.is_file() else "Run `job-apply-mcp init`."))
    if s.dry_run:
        out.append(Check("dry_run", OK, "dry_run = true (nothing is submitted)"))
    else:
        out.append(Check("dry_run", WARN, "dry_run = false: real submissions are enabled",
                         "Set dry_run = true in config.toml until you trust your profile and controls."))
    rf = s.control.rules_file
    if rf:
        p = Path(rf).expanduser()
        try:
            tomllib.loads(p.read_text(encoding="utf-8"))
            out.append(Check("controls", OK, f"rules file {p} parses"))
        except FileNotFoundError:
            out.append(Check("controls", FAIL, f"control.rules_file not found: {p}",
                             "Create it (copy examples/controls.example.toml) or remove the setting."))
        except tomllib.TOMLDecodeError as e:
            out.append(Check("controls", FAIL, f"{p}: {e}", "Fix the TOML syntax."))
    else:
        out.append(Check("controls", INFO, "no custom rules file; built-in controls only"))
    return out, s


def _profile_checks(s: Settings) -> list[Check]:
    p = Path(s.paths.profile).expanduser()
    try:
        prof = load_profile(p)
    except FileNotFoundError:
        return [Check("profile", FAIL, f"not found: {p}", "Run `job-apply-mcp init` or copy examples/profile.example.toml.")]
    except (ValidationError, tomllib.TOMLDecodeError) as e:
        return [Check("profile", FAIL, f"{p}: {str(e).splitlines()[0]}", "Fix the TOML/field named above.")]
    c = prof.contact
    missing = [k for k in ("first_name", "last_name", "email", "phone", "city") if not getattr(c, k)]
    if not prof.education:
        missing.append("education")
    out = []
    if (c.first_name, c.last_name) == ("Alex", "Rivera"):
        out.append(Check("profile", WARN, f"{p} still contains the fictional example person",
                         "Edit contact, education and work with your real details."))
    elif missing:
        out.append(Check("profile", WARN, f"{p} missing: {', '.join(missing)}", "Fill those fields in profile.toml."))
    else:
        out.append(Check("profile", OK, f"{p} complete ({c.full_name})"))
    if s.paths.resume:
        rp = Path(s.paths.resume).expanduser()
        out.append(Check("resume", OK, f"{rp}") if rp.is_file()
                   else Check("resume", FAIL, f"paths.resume not found: {rp}", "Point paths.resume at your PDF."))
    else:
        out.append(Check("resume", WARN, "paths.resume not set; applications that need an upload will stop",
                         'Set paths.resume = "/path/to/resume.pdf" in config.toml.'))
    return out


def _data_dir_check(s: Settings) -> Check:
    d = Path(s.paths.data_dir).expanduser()
    try:
        d.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=d, prefix=".doctor-", delete=True):
            pass
        return Check("data_dir", OK, f"{d} writable")
    except OSError as e:
        return Check("data_dir", FAIL, f"{d} not writable ({e.strerror or e})",
                     "Set paths.data_dir to a writable folder or fix its permissions.")


def _model_checks(s: Settings, network: bool) -> list[Check]:
    tiers = {**{f"tier:{k}": v for k, v in s.llm.tiers.items()},
             **{f"task:{k}": v for k, v in s.llm.task_tiers.items()}}
    out: list[Check] = []
    cache: dict[str, list[str] | None] = {}

    def listing(provider: str) -> list[str] | None:
        if provider in cache:
            return cache[provider]
        cfg = s.llm.providers.get(provider, {})
        names = None
        try:
            if provider == "ollama":
                r = httpx.get(f"{cfg.get('base_url', 'http://127.0.0.1:11434').rstrip('/')}/api/tags", timeout=2)
                r.raise_for_status()
                names = [m["name"] for m in r.json().get("models", [])]
            elif provider == "openai_compat" and cfg.get("base_url"):
                key = os.environ.get(cfg.get("api_key_env") or "", "")
                r = httpx.get(f"{cfg['base_url'].rstrip('/')}/models", timeout=3,
                              headers={"Authorization": f"Bearer {key}"} if key else {})
                r.raise_for_status()
                names = [m["id"] for m in r.json().get("data", [])]
        except Exception:  # noqa: BLE001
            names = None
        cache[provider] = names
        return names

    for label, t in tiers.items():
        if t.provider in ("client", "fake"):
            out.append(Check(f"model {label}", INFO, "answered by your MCP client (no local model needed)"))
        elif t.provider in ("ollama", "openai_compat"):
            if not network:
                out.append(Check(f"model {label}", INFO, f"{t.provider}:{t.model} (network checks skipped)"))
                continue
            names = listing(t.provider)
            if names is None:
                fix = ("Start Ollama (`ollama serve`) or install it from https://ollama.com." if t.provider == "ollama"
                       else "Start your OpenAI-compatible server and set llm.providers.openai_compat.base_url.")
                out.append(Check(f"model {label}", WARN, f"{t.provider} unreachable; falls back to other tiers/client", fix))
            elif t.model in names or (t.provider == "ollama" and f"{t.model}:latest" in names):
                out.append(Check(f"model {label}", OK, f"{t.provider}:{t.model} available"))
            else:
                fix = f"ollama pull {t.model}" if t.provider == "ollama" else f"load {t.model} in your server"
                out.append(Check(f"model {label}", WARN, f"{t.provider}:{t.model} not found", fix))
        elif t.provider in ("anthropic", "litellm"):
            env = s.llm.providers.get(t.provider, {}).get("api_key_env", "ANTHROPIC_API_KEY")
            out.append(Check(f"model {label}", OK if os.environ.get(env) else WARN,
                             f"{t.provider}:{t.model} ({env} {'set' if os.environ.get(env) else 'not set'})",
                             "" if os.environ.get(env) else f"export {env}=... (never put keys in config files)"))
    return out


def _browser_checks(s: Settings) -> list[Check]:
    out: list[Check] = []
    try:
        import playwright  # noqa: F401
    except ImportError:
        pw = False
    else:
        pw = True
    chrome = find_system_chrome()
    if not pw:
        out.append(Check("playwright", WARN, "Playwright not installed (browser applying unavailable; API-based "
                         "platforms still work)", "pip install 'job-apply-mcp[browser]'  (or uvx --with playwright ...)"))
    else:
        out.append(Check("playwright", OK, "Playwright installed"))
        if s.browser.channel in ("chrome", "msedge") and not chrome and s.browser.channel == "chrome":
            out.append(Check("browser", FAIL, "browser.channel = 'chrome' but no system Chrome found",
                             "Install Chrome or clear browser.channel and run `playwright install chromium`."))
        elif chrome:
            out.append(Check("browser", OK, f"system Chrome: {chrome}"))
        else:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    exe = Path(p.chromium.executable_path)
                out.append(Check("browser", OK, f"Playwright Chromium at {exe}") if exe.is_file()
                           else Check("browser", WARN, "Playwright Chromium not downloaded",
                                      "playwright install chromium"))
            except Exception as e:  # noqa: BLE001
                out.append(Check("browser", WARN, f"could not inspect Playwright browsers ({type(e).__name__})",
                                 "playwright install chromium"))
    return out


def run_checks(network: bool = True) -> list[Check]:
    ram = total_ram_gb()
    checks = [Check("system", INFO, f"job-apply-mcp {__version__}, Python {platform.python_version()}, "
                    f"{platform.system()} {platform.machine()}, {'?' if ram is None else f'{ram:g}'} GB RAM")]
    cfg_checks, s = _config_checks()
    checks += cfg_checks
    if s is not None:
        checks += _profile_checks(s)
        checks.append(_data_dir_check(s))
        checks += _model_checks(s, network)
        checks += _browser_checks(s)
    return checks


def run_doctor(*, as_json: bool = False, network: bool = True, out=print) -> int:
    checks = run_checks(network)
    if as_json:
        out(json.dumps([asdict(c) for c in checks], indent=1))
    else:
        tag = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", INFO: "[info]"}
        for c in checks:
            out(f"{tag[c.status]} {c.name}: {c.detail}")
            if c.fix and c.status in (WARN, FAIL):
                out(f"         fix: {c.fix}")
        n = {k: sum(c.status == k for c in checks) for k in (OK, WARN, FAIL)}
        out(f"\n{n[OK]} ok, {n[WARN]} warning(s), {n[FAIL]} failure(s)")
    return 1 if any(c.status == FAIL for c in checks) else 0
