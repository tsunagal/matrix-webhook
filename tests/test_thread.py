import asyncio
import os

os.environ.setdefault("AS_TOKEN", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mautrix.client import SyncStream  # noqa: E402
from mautrix.errors import MForbidden, MLimitExceeded, MNotFound, MUnknown  # noqa: E402

import crypto  # noqa: E402
import main  # noqa: E402


class FakeApi:
    def __init__(self, events):
        self.events = events

    async def request(self, method, path):
        event_id = str(path).rsplit("/", 1)[-1].replace("%24", "$")
        if event_id not in self.events:
            raise MNotFound(404, "Event not found")
        return self.events[event_id]


class FakeClient:
    def __init__(self, events=None, fail_body=None, join_error=None):
        self.sent = []
        self.redacted = []
        self.api = FakeApi(events or {})
        self.fail_body = fail_body
        self.join_error = join_error

    async def resolve_room_alias(self, alias):
        raise MNotFound(404, "Room alias not found")

    async def join_room(self, room_id, max_retries=0):
        if self.join_error:
            raise self.join_error
        return room_id

    async def send_message_event(self, room_id, event_type, content):
        if self.fail_body and self.sent:
            raise self.fail_body
        self.sent.append(content)
        return f"$e{len(self.sent)}"

    async def redact(self, room_id, event_id, reason=None):
        self.redacted.append(event_id)


@pytest.fixture
def hook(monkeypatch):
    def make(**kw):
        client = FakeClient(**kw)

        class FakeBot:
            pass

        FakeBot.client = client
        monkeypatch.setattr(main, "get_bot", lambda: FakeBot())
        return TestClient(main.app), client
    return make


def post(http, **body):
    return http.post("/webhook", json={"room_id": "!r:s", **body})


@pytest.mark.parametrize("text,title,root,body", [
    ("Deploy done", "", "Deploy done", "Deploy done"),
    ("\n  Deploy   done \n\n- a\n- b\n", "", "Deploy done", "- a\n- b"),
    ("x" * 61 + "\nmore", "", "x" * 59 + "…", "x" * 61 + "\nmore"),
    ("詳細", "障害 発生", "障害 発生", "詳細"),
    ("   ", "Title", "Title", main.PLACEHOLDER),
    ("Alert\r\nline 2\rline 3", "", "Alert", "line 2\nline 3"),
])
def test_root_and_body(text, title, root, body):
    assert main.split_post(text, title, html=False) == (root, body)


def test_html_root_is_plain_text_and_body_keeps_html():
    assert main.split_post("<p><b>Alert</b> high</p><p>cpu 99%</p>", "", html=True) == (
        "Alert high", "<p><b>Alert</b> high</p><p>cpu 99%</p>")


@pytest.mark.parametrize("line,expected", [
    ("a" * 58 + "👍🏽b", "a" * 58 + "…"),
    ("a" * 58 + "👨‍👩‍👧", "a" * 58 + "…"),
    ("a" * 58 + "🇯🇵🇺🇸", "a" * 58 + "…"),
    ("a" * 57 + "🇯🇵🇺🇸", "a" * 57 + "🇯🇵…"),
    ("a" * 58 + "😀😀😀", "a" * 58 + "😀…"),
    ("\u0301" * 70, "\u0301" * 59 + "…"),
])
def test_cut_title_keeps_emoji_whole(line, expected):
    assert main.cut_title(line) == (expected, True)


def test_new_thread_sends_root_then_reply(hook):
    http, client = hook()
    r = post(http, text="Title\nBody")
    assert r.json() == {"ok": True, "event_id": "$e2", "thread_root": "$e1"}
    assert client.sent[0] == {"msgtype": "m.text", "body": "Title", "m.mentions": {}}
    assert client.sent[1] == {"msgtype": "m.text", "body": "Body", "m.relates_to": {
        "rel_type": "m.thread", "event_id": "$e1", "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$e1"}}}


def test_title_field_is_collapsed_and_text_is_the_body(hook):
    http, client = hook()
    post(http, title=" 障害\n 発生 ", text="詳細")
    assert [c["body"] for c in client.sent] == ["障害 発生", "詳細"]


def test_thread_false_sends_one_flat_message(hook):
    http, client = hook()
    assert post(http, text="a\nb", thread=False).json() == {"ok": True, "event_id": "$e1", "thread_root": None}
    assert client.sent == [{"msgtype": "m.text", "body": "a\nb"}]


def test_reply_into_existing_thread(hook):
    http, client = hook(events={"$root": {"type": "m.room.message", "content": {"body": "t"}, "unsigned": {}}})
    assert post(http, text="more", thread_ts="$root").json() == {"ok": True, "event_id": "$e1", "thread_root": "$root"}
    assert client.sent[0]["m.relates_to"]["event_id"] == "$root"


@pytest.mark.parametrize("events,status", [
    ({}, 404),
    ({"$root": {"type": "m.room.message", "content": {"m.relates_to": {"rel_type": "m.thread"}}}}, 400),
    ({"$root": {"type": "m.room.message", "content": {}, "unsigned": {"redacted_because": {}}}}, 400),
    ({"$root": {"type": "m.room.name", "state_key": "", "content": {}}}, 400),
])
def test_bad_thread_root_is_rejected(hook, events, status):
    http, client = hook(events=events)
    r = post(http, text="x", thread_root="$root")
    assert r.status_code == status and r.json()["ok"] is False
    assert client.sent == []


@pytest.mark.parametrize("kwargs", [
    {"content": b"{not json"},
    {"content": b"[1, 2]"},
    {"data": {"text": "x", "room_id": "!r:s"}},
    {"json": {"room_id": "!r:s", "text": 5}},
    {"json": {"room_id": "!r:s", "text": {"a": 1}}},
    {"json": {"room_id": "!r:s", "text": ["a"]}},
    {"json": {"room_id": "!r:s", "text": "x", "title": 1}},
    {"json": {"room_id": "!r:s", "text": "x", "thread": "no"}},
    {"json": {"room_id": "!r:s", "text": "x", "thread_root": "$a", "thread_ts": "$b"}},
    {"json": {"room_id": "!r:s", "text": "   "}},
    {"json": {"room_id": 5, "text": "x"}},
    {"json": {"channel": ["#a:s"], "text": "x"}},
])
def test_invalid_requests_are_400(hook, kwargs):
    http, client = hook()
    r = http.post("/webhook", **kwargs)
    assert r.status_code == 400 and r.json()["ok"] is False
    assert client.sent == []


def test_room_errors_keep_their_status(hook):
    http, _ = hook()
    assert post(http, text="x", channel="#missing:s").status_code == 404
    http, _ = hook(join_error=MForbidden(403, "You are not invited"))
    assert post(http, text="x").status_code == 403


def test_other_join_errors_still_send(hook):
    http, client = hook(join_error=MLimitExceeded(429, "slow down"))
    assert post(http, text="x").status_code == 200 and len(client.sent) == 2


@pytest.mark.parametrize("error", [MUnknown(500, "boom"), TimeoutError(), RuntimeError("crypto")])
def test_failed_body_redacts_the_new_root(hook, error):
    http, client = hook(fail_body=error)
    r = post(http, text="Title\nBody")
    assert r.status_code == 500 and r.json()["ok"] is False
    assert client.redacted == ["$e1"]


def test_invite_is_only_handled_from_the_invite_section():
    joined = []

    class Client:
        mxid = "@bot:s"

        async def join_room(self, room_id):
            joined.append(room_id)

    class Evt:
        state_key = "@bot:s"
        room_id = "!r:s"

        class content:
            membership = crypto.Membership.INVITE

    bot = object.__new__(crypto.Bot)
    bot.client = Client()
    for source in (SyncStream.INVITED_ROOM | SyncStream.STATE, SyncStream.JOINED_ROOM | SyncStream.TIMELINE):
        Evt.source = source
        asyncio.run(bot._on_member(Evt))
    assert joined == ["!r:s"]


def test_slack_timestamp_thread_ts_is_ignored(hook):
    for ts in ("1503435956.000247", 1503435956.000247):
        http, client = hook()
        assert post(http, text="t\nb", thread_ts=ts).json()["thread_root"] == "$e1"
        assert len(client.sent) == 2


def test_explicit_title_is_capped(hook):
    http, client = hook()
    post(http, title="t" * 80, text="body")
    assert client.sent[0]["body"] == "t" * 59 + "…"


def test_table_cells_are_separated():
    assert main.html_to_text("<table><tr><td>a</td><td>b</td></tr></table>") == "a b"


def test_unknown_path_and_method_use_the_json_error_shape(hook):
    http, _ = hook()
    for r in (http.get("/nope"), http.get("/webhook")):
        assert r.status_code in (404, 405) and r.json()["ok"] is False


def test_failed_cleanup_does_not_mask_the_startup_error(monkeypatch):
    class Bot:
        async def start(self):
            raise ConnectionError("synapse down")

        async def stop(self):
            raise RuntimeError("cleanup")

    monkeypatch.setattr(crypto, "Bot", Bot)
    with pytest.raises(ConnectionError):
        asyncio.run(crypto.start_bot())
    assert crypto._bot is None


@pytest.mark.parametrize("body", [{"thread": False}, {"thread_root": "$root"}])
def test_non_matrix_send_error_without_new_root_is_json(hook, body):
    http, client = hook(events={"$root": {"type": "m.room.message", "content": {}, "unsigned": {}}},
                        fail_body=TimeoutError())
    client.sent.append("primed")
    r = post(http, text="x", **body)
    assert r.status_code == 500 and r.json()["ok"] is False and client.redacted == []
