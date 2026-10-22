"""Privacy scanner tests. All data is synthetic; secrets are assembled at runtime to avoid self-triggering."""
import importlib.util
import subprocess
from pathlib import Path

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
