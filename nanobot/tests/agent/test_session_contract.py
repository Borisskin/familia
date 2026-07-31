import json

import nanobot.session.manager as session_manager_module
from nanobot.session.manager import Session, SessionManager


_REMOVED_SESSION_FIELDS = {
    "session_generation_id",
    "next_message_seq",
    "session_revision",
}


def test_session_model_has_no_coordinate_fields() -> None:
    session = Session(key="cli:test")
    session.add_message("user", "hello")

    assert _REMOVED_SESSION_FIELDS.isdisjoint(session.__dataclass_fields__)
    assert "message_seq" not in session.messages[0]


def test_session_serialization_has_no_coordinate_fields(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:test")
    session.add_message("user", "hello")

    manager.save(session)

    path = manager._get_session_path(session.key)
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records
    assert all(_REMOVED_SESSION_FIELDS.isdisjoint(record) for record in records)
    assert all("message_seq" not in record for record in records)


def test_session_manager_has_no_coordinate_exception() -> None:
    assert not hasattr(session_manager_module, "_SessionCoordinateError")
