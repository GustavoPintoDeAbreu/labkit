import pytest


@pytest.fixture(autouse=True)
def _no_real_chrome(monkeypatch):
    """fetch_post tries his Chrome first (lab-browser on the PC); tests never reach the real one.
    The Chrome tests turn it back on with a fake (tests/test_reddit_chrome.py)."""
    from labkit import reddit
    monkeypatch.setattr(reddit, "LAB_BROWSER_MCP", "0")
