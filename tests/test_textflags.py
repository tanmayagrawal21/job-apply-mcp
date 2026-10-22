from job_apply_mcp.platforms import textflags as t


def test_html_to_text_handles_escaped_html():
    assert t.html_to_text("&lt;p&gt;Hello&lt;/p&gt;&lt;ul&gt;&lt;li&gt;A&lt;/li&gt;&lt;li&gt;B &amp;amp; C&lt;/li&gt;&lt;/ul&gt;") == "Hello A B & C"
    assert t.html_to_text("<p>x</p><p>y</p>") == "x y" and t.html_to_text(None) == ""


def test_years():
    txt = "Requires 3-5 years of relevant experience, or 5+ years working with Go experience. Minimum of 2 years."
    assert t.years_mentions(txt) == [3, 5, 2] and t.years_required(txt) == 2
    assert t.years_required("We were founded 20 years ago; 2026 is great") is None
    assert t.years_required("0-2 years of experience") is None  # zero lower bound = no requirement


def test_sponsorship_and_citizenship():
    assert "no_sponsorship" in t.sponsorship_flags("We are unable to provide visa sponsorship.")
    assert "no_sponsorship" in t.sponsorship_flags("Sponsorship is not available for this role")
    assert not t.sponsorship_flags("We will sponsor visas for the right candidate")
    assert "opt_excluded" in t.sponsorship_flags("STEM OPT candidates are not eligible")
    assert t.citizenship_flags("Active TS/SCI clearance required") and t.citizenship_flags("Must be a U.S. citizen")
    assert not t.citizenship_flags("Great benefits")


def test_grad_windows_and_conflict():
    w = t.grad_windows("Open to students graduating in December 2026 or 2027.")
    assert w and t.grad_window_conflict(w, "2026-05") is True
    w2 = t.grad_windows("for candidates graduating between Spring 2026 and Fall 2026")
    assert t.grad_window_conflict(w2, "2026-05") is False  # mentions an on-or-before window
    assert t.grad_window_conflict(t.grad_windows("Class of 2026"), "2026-05") is False
    assert t.grad_window_conflict(t.grad_windows("Class of 2028 graduates"), "2026-05") is True
    assert t.grad_window_conflict([], "2026-05") is False and t.grad_window_conflict(["Class of 2030"], "bad") is False


def test_salary():
    s = t.extract_salary("The range is $120,000 - $150,000 per year plus equity")
    assert (s.min, s.max, s.period) == (120000, 150000, "year") and s.summary() == "$120,000-$150,000"
    s = t.extract_salary("Pay: $95k-$120k")
    assert (s.min, s.max) == (95000, 120000)
    s = t.extract_salary("$45 to $60 per hour")
    assert s.period == "hour" and s.annual() == (93600, 124800) and s.summary() == "$45-$60/hr"
    assert t.extract_salary("Founded in 2010-2015 era, 401(k) match") is None
    assert t.extract_salary("no pay info") is None


def test_flag_text_combined():
    f = t.flag_text("PhD required. Must be a US citizen. 5 years of experience. University of Nowhere. "
                    "Graduating in 2027. No sponsorship.")
    assert set(f) == {"phd_only", "citizenship", "years", "academic", "grad_window", "no_sponsorship"}
    assert t.flag_text("A friendly place to work.") == {}
