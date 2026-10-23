"""Privacy scanner tests. All data is synthetic; secrets are assembled at runtime to avoid self-triggering."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("privacy_scan", Path(__file__).parent.parent / "scripts" / "privacy_scan.py")
ps = importlib.util.module_from_spec(spec)
import sys

sys.modules["privacy_scan"] = ps
spec.loader.exec_module(ps)


def kinds(line, terms=()):
    return {k for k, _ in ps.scan_line(line, list(terms))}


def test_email_rules():
    assert "email address" in kinds("mail jane.roe@corp-mail.io now")
    assert not kinds("a@example.com b@x.test c@y.invalid noreply@foo.io")


def test_phone_and_address():
    assert "phone number" in kinds("call (520) 123-4567")
    assert "phone number" in kinds("call +44 20 7946 0958")
    assert "phone number" not in kinds("call 555-0100 or 2026-10-22")
    assert "street address" in kinds("lives at 123 Maple Street")


def test_secrets():
    assert "secret: github token" in kinds("t=" + "ghp_" + "a1B2c3D4" * 5)
    assert "secret: aws access key id" in kinds("AKIA" + "ABCDEFGHIJKLMNOP")
    assert "secret: openai/anthropic-style key" in kinds("sk-" + "ant" + "x9Qz" * 8)
    assert "secret: private key header" in kinds("-----BEGIN RSA " + "PRIVATE KEY-----")
    assert "secret: high-entropy key=value" in kinds('api_key = "Zk3v9QpX7mL2aR8tYw4N"')
    assert not kinds("api_key = os.environ_value_name")


def test_home_path():
    assert "absolute home path" in kinds("/Users/janedoe/Documents")
    assert not kinds("/Users/<name>/x and /home/runner/work")


def test_private_terms_never_printed():
    hits = ps.scan_text("a.py", [(1, "I worked at Zebracorp last year")], ["zebracorp"], [])
    assert hits[0].kind == "private term #1"
    assert "zebracorp" not in str(hits[0]).lower()


def test_allowlist_and_filetype():
    import re
    allow = [re.compile(r"^fx\.py:")]
    assert not ps.scan_text("fx.py", [(1, "x@corp-mail.io")], [], allow)
    assert ps.check_file_type("docs/cv.pdf", [])
    assert not ps.check_file_type("tests/fixtures/cv.pdf", [])


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.com", "-c", "user.name=t", *a],
                   check=True, capture_output=True)


def test_diff_only_scans_added_lines(tmp_path, capsys):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "old.txt").write_text("legacy x@corp-mail.io\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    assert ps.main(["--root", str(tmp_path), "--diff", "main"]) == 0
    (tmp_path / "new.txt").write_text("ok\nreach me at y@corp-mail.io\n")
    (tmp_path / "r.docx").write_bytes(b"x")
    _git(tmp_path, "add", "-A")
    assert ps.main(["--root", str(tmp_path), "--staged"]) == 1
    out = capsys.readouterr().out
    assert "new.txt:2" in out and "old.txt" not in out and "r.docx" in out


@pytest.fixture(autouse=True)
def _no_real_allow_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "DEFAULT_ACCEPT_FILE", tmp_path / "absent-allow.txt")


def _repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_allow_file_accepts_and_never_prints(tmp_path, capsys):
    repo = _repo(tmp_path, {
        "LICENSE": "Copyright (c) 2026 Quokka Fernbrook\n",
        "README.md": "See https://github.com/quokkaf/demo-repo for details\n",
    })
    terms = tmp_path / "terms.txt"
    terms.write_text("quokka fernbrook\nquokkaf\n")
    base = ["--root", str(repo), "--terms", str(terms)]
    assert ps.main(base) == 1  # no allow file: unchanged behaviour
    capsys.readouterr()
    allow = tmp_path / "allow.txt"
    allow.write_text("# comment\n@salt 9f2c4e7a1b3d5f6081a2c3e4f5061728\n@urls\n")
    assert ps.main([*base, "--allow", str(allow), "--fingerprint", "1"]) == 0
    fp = capsys.readouterr().out.strip()
    assert fp == ps.fingerprint("9f2c4e7a1b3d5f6081a2c3e4f5061728", "quokka fernbrook") and "quokka" not in fp
    allow.write_text(f"# comment\n@salt 9f2c4e7a1b3d5f6081a2c3e4f5061728\nLICENSE\t{fp}\n@urls\n")
    assert ps.main([*base, "--allow", str(allow)]) == 0
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert "2 accepted" in out
    assert "quokka" not in out.lower() and "fernbrook" not in out.lower()


def test_allow_file_partial_still_fails(tmp_path, capsys):
    repo = _repo(tmp_path, {
        "LICENSE": "Copyright (c) 2026 Quokka Fernbrook\n",
        "notes.md": "quokkaf outside a URL, and https://github.com/quokkaf/x\n",
    })
    terms = tmp_path / "terms.txt"
    terms.write_text("quokka fernbrook\nquokkaf\n")
    allow = tmp_path / "allow.txt"
    allow.write_text("LICENSE\t*\n@urls\n")
    assert ps.main(["--root", str(repo), "--terms", str(terms), "--allow", str(allow)]) == 1
    cap = capsys.readouterr()
    assert "notes.md:1: [private term #2]" in cap.out and "LICENSE" not in cap.out
    assert "1 accepted" in cap.err
    assert "quokka" not in (cap.out + cap.err).lower()


def test_allow_rules_scope_and_reorder():
    fp = ps.fingerprint("c0ffee42beef1337d00dfacecafe9a8b", "wombatico")
    rules = ps.AcceptRules([("docs/*.md", fp)], salt="c0ffee42beef1337d00dfacecafe9a8b")
    rules.bind(["otherterm", "wombatico"])
    assert rules.accepts("docs/a.md", "private term #2", (0, 3), "abc")
    assert not rules.accepts("docs/a.md", "private term #1", (0, 3), "abc")
    rules.bind(["wombatico", "otherterm"])  # terms file reordered: rule follows the term, not the index
    assert rules.accepts("docs/a.md", "private term #1", (0, 3), "abc")
    assert not rules.accepts("docs/a.md", "private term #2", (0, 3), "abc")
    assert not rules.accepts("src/a.md", "private term #2", (0, 3), "abc")
    assert not rules.accepts("docs/a.md", "email address", (0, 3), "abc")


def test_mask_merges_overlaps():
    assert ps.mask_spans("abcdefghij", [(2, 5), (4, 8)]) == "ab***ij"


def test_urls_accepts_only_owner_segment():
    rules = ps.AcceptRules([], urls=True)
    rules.bind(["zephyrine"])

    def accepted(line):
        hits = ps.scan_line(line, ["zephyrine"])
        return [rules.accepts("README.md", k, sp[0], line) for k, sp in hits if k.startswith("private term")]

    assert accepted("see https://github.com/zephyrine/job-apply-mcp") == [True]
    assert accepted("https://github.com/Zephyrine") == [True]
    assert accepted("https://github.com/acme/x/issues?q=Zephyrine+Quill+lives+at+12+Elm") == [False]
    assert accepted("https://github.com/acme/zephyrine-tools") == [False]
    assert accepted("https://github.com/zephyrine21/x") == [True]  # term inside the owner handle
    assert accepted("zephyrine at https://example.com/zephyrine") == [False, False]


def test_diff_and_staged_ignore_allow_file(tmp_path, capsys):
    repo = _repo(tmp_path, {"README.md": "base\n"})
    terms = tmp_path / "terms.txt"
    terms.write_text("quokkaf\n")
    allow = tmp_path / "allow.txt"
    allow.write_text("*\t*\n@urls\n")
    (repo / "README.md").write_text("base\nsee https://github.com/quokkaf/demo\n")
    _git(repo, "add", "-A")
    args = ["--root", str(repo), "--terms", str(terms), "--allow", str(allow)]
    assert ps.main([*args, "--staged"]) == 1
    _git(repo, "commit", "-qm", "add url")
    assert ps.main([*args, "--diff", "HEAD~1"]) == 1
    assert ps.main(args) == 0  # whole tree: accepted
    out = capsys.readouterr()
    assert out.out.count("README.md:2: [private term #1]") == 2 and "quokkaf" not in (out.out + out.err)


def test_allow_file_inside_git_worktree_is_refused(tmp_path, capsys):
    repo = _repo(tmp_path, {"README.md": "ok\n"})
    inside = repo / "sub" / "privacy_allow.txt"
    inside.parent.mkdir()
    inside.write_text("@urls\n")
    assert ps.main(["--root", str(repo), "--allow", str(inside)]) == 2
    assert "inside a git work tree" in capsys.readouterr().err


def test_short_or_weak_salt_is_refused(tmp_path, capsys):
    repo = _repo(tmp_path, {"README.md": "ok\n"})
    allow = tmp_path / "allow.txt"
    for salt in ("pepper", "0123456789abcdef", "a" * 64, "c2hvcnQ="):
        allow.write_text(f"@salt {salt}\n@urls\n")
        assert ps.main(["--root", str(repo), "--allow", str(allow)]) == 2, salt
        assert "salt" in capsys.readouterr().err
    allow.write_text(f"LICENSE\t{'ab' * 6}\n")  # fingerprint rule without a salt
    assert ps.main(["--root", str(repo), "--allow", str(allow)]) == 2


def test_long_salts_accepted():
    assert ps.salt_ok("9f2c4e7a1b3d5f6081a2c3e4f5061728")  # 16 bytes hex
    assert ps.salt_ok("q83vEjRWeJq83vEjRWeJqw==")  # 16 bytes base64
    assert ps.salt_ok("q83vEjRWeJq83vEjRWeJq-_x")  # url-safe base64
    assert not ps.salt_ok("9f2c4e7a1b3d5f6081a2c3e4f50617")  # 15 bytes
