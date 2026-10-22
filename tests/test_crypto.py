import asyncio
import os

os.environ.setdefault("AS_TOKEN", "test")

import crypto  # noqa: E402
import main  # noqa: E402


def test_sync_filter_keeps_what_encryption_needs():
    room = crypto.SYNC_FILTER.serialize()["room"]
    for section in ("state", "timeline"):
        assert set(room[section]["types"]) == {
            "m.room.member",
            "m.room.encryption",
            "m.room.power_levels",
        }
        assert room[section]["lazy_load_members"] is False


def test_sync_filter_drops_messages_and_presence():
    serialized = crypto.SYNC_FILTER.serialize()
    assert "m.room.message" not in serialized["room"]["timeline"]["types"]
    assert serialized["presence"]["not_types"] == ["*"]
    assert serialized["room"]["ephemeral"]["not_types"] == ["*"]


def test_room_id_targets_are_used_as_is():
    assert asyncio.run(main.resolve_room("!abc:example.org")) == "!abc:example.org"


def test_bot_is_not_available_before_startup():
    try:
        crypto.get_bot()
    except RuntimeError:
        return
    raise AssertionError("get_bot() should fail before start_bot()")
