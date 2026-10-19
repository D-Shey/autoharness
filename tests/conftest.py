import pytest

from autoharness import agents, cli

# the areas and method a test brief names: real ones need searches run against the network
TEST_DOMAINS = "layout: create test-layout (searched: test); checks: create test-checks (searched: test)"
TEST_METHOD = "build, then compare: https://example.com/guide/how; searched: how it is done"


@pytest.fixture(autouse=True)
def no_agent_session(monkeypatch):
    """Tests run inside an agent session as often as not: its id would make every test task that session's."""
    for var in agents._SESSION.values():
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def test_domains_need_no_search(monkeypatch):
    """A test brief's areas stand without the searches a real task runs; any other areas are checked as usual."""
    real = cli._check_domains
    monkeypatch.setattr(cli, "_check_domains",
                        lambda text, *a: text if text == TEST_DOMAINS else real(text, *a))


@pytest.fixture(autouse=True)
def no_reviewer_to_start(monkeypatch):
    """Whether an agent CLI is installed differs from machine to machine: tests start none unless they say so."""
    monkeypatch.setattr(cli, "_reviewer_can_start", lambda agent: False)
