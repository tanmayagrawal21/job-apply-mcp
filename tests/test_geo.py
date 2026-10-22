from job_apply_mcp.apply.geo import location_queries, pick_location


def test_queries_shorten_and_expand_state():
    assert location_queries("Springfield, IL") == ["Springfield, IL", "Springfield, Illinois", "Springfield"]
    assert location_queries("Springfield") == ["Springfield"]


def test_pick_matches_state_abbreviation_or_name():
    opts = ["Springfield, Missouri, United States", "Springfield, Illinois, United States"]
    assert pick_location("Springfield, IL", opts) == "Springfield, Illinois, United States"
    assert pick_location("Springfield, Illinois", opts) == "Springfield, Illinois, United States"


def test_pick_never_contradicts_given_state():
    assert pick_location("Springfield, IL", ["Springfield, Missouri, United States"]) is None
    assert pick_location("Springfield, IL", ["Chicago, Illinois, United States"]) is None


def test_pick_city_only_takes_first_city_match():
    assert pick_location("Springfield", ["Chicago, IL", "Springfield, Ohio"]) == "Springfield, Ohio"


def test_job_alert_banner_is_not_a_validation_error():
    from job_apply_mcp.apply.browser_playwright import _BENIGN_ALERT
    assert _BENIGN_ALERT.search("alert:Create a Job Alert\n\nInterested in building your career at X?")
    assert not _BENIGN_ALERT.search("alert:This field is required")
