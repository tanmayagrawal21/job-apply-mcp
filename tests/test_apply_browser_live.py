"""Real-browser smoke test against a local fixture form. Run with: JAM_LIVE=1 uv run pytest -m live
(needs `uv sync --extra browser` and either `playwright install chromium` or BROWSER_CHANNEL=chrome)."""
import http.server
import os
import threading

import pytest

from job_apply_mcp.apply.browser_playwright import PlaywrightDriver
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, FieldKind, FormField

pytestmark = pytest.mark.live

PAGE = b"""<html><body><form onsubmit="location='/acme/confirmation'; return false;">
<label for="first_name">First Name *</label><input id="first_name" name="first_name">
<label for="email">Email</label><input id="email" name="email">
<label for="src">Source</label><select id="src" name="src"><option value="">--</option>
<option>LinkedIn</option><option>Other</option></select>
<label for="resume">Resume</label><input type="file" id="resume" name="resume">
<button type="submit">Submit application</button></form></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><body>Thank you for applying</body></html>" if "confirmation" in self.path else PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


async def test_fill_verify_submit_local_form(tmp_path):
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = Settings()
    s.browser.headless = True
    s.browser.user_data_dir = str(tmp_path / "profile")
    s.browser.channel = os.environ.get("BROWSER_CHANNEL")
    s.browser.confirmation_wait_s = 5
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"%PDF-1.4")
    form = ApplicationForm(ref="greenhouse:acme:1", fields=[
        FormField(key="first_name", label="First Name *", kind=FieldKind.text, required=True),
        FormField(key="email", label="Email", kind=FieldKind.text),
        FormField(key="src", label="Source", kind=FieldKind.select, options=["LinkedIn", "Other"]),
        FormField(key="resume", label="Resume", kind=FieldKind.file)])
    drv = PlaywrightDriver(s)
    url = f"http://127.0.0.1:{srv.server_port}/acme/form"
    try:
        ans = {"first_name": "Ada", "email": "ada@example.com", "src": "Other"}
        dry = await drv.fill_and_submit(url, form, ans, {"resume": str(resume)}, dry_run=True)
        assert dry.status == "dry_run", dry.detail
        res = await drv.fill_and_submit(url, form, ans, {"resume": str(resume)}, dry_run=False)
        assert res.status == "submitted", res.detail
    finally:
        await drv.aclose()
        srv.shutdown()
