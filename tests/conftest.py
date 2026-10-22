import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("JAM_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test; set JAM_LIVE=1")
    for it in items:
        if "live" in it.keywords:
            it.add_marker(skip)
