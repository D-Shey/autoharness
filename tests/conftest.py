import pytest

from autoharness import agents


@pytest.fixture(autouse=True)
def no_agent_session(monkeypatch):
    """Tests run inside an agent session as often as not: its id would make every test task that session's."""
    for var in agents._SESSION.values():
        monkeypatch.delenv(var, raising=False)
