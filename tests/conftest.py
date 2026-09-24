import pytest


@pytest.fixture(autouse=True)
def private_runner_lock(monkeypatch, tmp_path):
    """Unit tests never contend with a real comparison's machine-wide runner lock."""
    from qtb import coordinator
    from qtb.coordinator import costs

    lock = tmp_path / "qtb-runner-test.lock"
    monkeypatch.setattr(costs, "runner_lock", lambda: lock)
    monkeypatch.setattr(coordinator, "runner_lock", lambda: lock)
