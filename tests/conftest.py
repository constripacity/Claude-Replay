import pytest
import claude_replay.store as store


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Give each test an isolated SQLite database in tmp_path."""
    db_path = tmp_path / "test-sessions.db"
    monkeypatch.setattr(store, "DB_PATH", str(db_path))
    monkeypatch.setattr(store, "_conn", None)
    yield db_path
    if store._conn is not None:
        store._conn.close()
        monkeypatch.setattr(store, "_conn", None)
