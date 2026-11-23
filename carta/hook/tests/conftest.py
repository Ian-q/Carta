import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_reach: run the hook's real backend reachability probe (carta.hook.reach)",
    )


@pytest.fixture(autouse=True)
def _no_real_reach_probe(request, monkeypatch):
    """Hook tests mock run_search and point at made-up remote hosts
    (`*.invalid`). The #106 connect probe would correctly report those as
    unreachable and skip the search under test, so it is stubbed to "all up"
    unless a test is marked `real_reach`."""
    if request.node.get_closest_marker("real_reach"):
        return
    monkeypatch.setattr("carta.hook.reach.probe", lambda *a, **k: [])
