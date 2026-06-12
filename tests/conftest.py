import pytest


@pytest.fixture
def monkeypatch_storage() -> dict:
    """Scratch dict for tests that stash original callables before patching
    module-level functions and restore them in a finally block."""
    return {}
