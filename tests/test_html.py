import os

os.environ.setdefault("AS_TOKEN", "test")

import main  # noqa: E402


def test_plain_text_is_sent_unchanged():
    assert main.build_content("<b>x</b>", html=False) == {"msgtype": "m.text", "body": "<b>x</b>"}


def test_script_and_event_handlers_are_removed():
    c = main.build_content('<b onclick="alert(1)">hi</b><script>alert(2)</script>', html=True)
    assert c["formatted_body"] == "<b>hi</b>"
    assert c["body"] == "hi"
    assert c["format"] == "org.matrix.custom.html"


def test_javascript_links_are_dropped():
    c = main.build_content('<a href="javascript:alert(1)">x</a> <a href="https://e.com">y</a>', html=True)
    assert "javascript" not in c["formatted_body"]
    assert '<a href="https://e.com">y</a>' in c["formatted_body"]


def test_unknown_tags_and_style_are_removed_but_text_kept():
    c = main.build_content('<iframe src="https://e.com"></iframe><p style="color:red">text</p>', html=True)
    assert c["formatted_body"] == "<p>text</p>"


def test_img_only_from_mxc():
    c = main.build_content('<img src="https://tracker.example/p.gif" alt="a"><img src="mxc://s/m" alt="b">', html=True)
    assert "tracker" not in c["formatted_body"]
    assert 'src="mxc://s/m"' in c["formatted_body"]


def test_matrix_attributes_kept():
    c = main.build_content('<span data-mx-color="#ff0000" data-mx-spoiler="">s</span><code class="language-py">x</code>', html=True)
    assert 'data-mx-color="#ff0000"' in c["formatted_body"]
    assert 'class="language-py"' in c["formatted_body"]


def test_body_is_plain_text_with_line_breaks():
    c = main.build_content("<p>Deploy <b>done</b> &amp; ok</p><ul><li>a</li><li>b</li></ul>line1<br>line2", html=True)
    assert c["body"] == "Deploy done & ok\n\n- a\n- b\nline1\nline2"


def test_webhook_endpoint_sends_sanitized_content(monkeypatch):
    from fastapi.testclient import TestClient

    sent = {}

    class FakeClient:
        async def join_room(self, room_id, max_retries=0):
            return room_id

        async def send_message_event(self, room_id, event_type, content):
            sent.update(content)
            return "$e"

    class FakeBot:
        client = FakeClient()

    monkeypatch.setattr(main, "get_bot", lambda: FakeBot())
    r = TestClient(main.app).post("/webhook", json={
        "room_id": "!r:s", "format": "html",
        "text": '<b>ok</b><img src=x onerror="alert(1)">',
    })
    assert r.json() == {"ok": True, "event_id": "$e"}
    assert sent["formatted_body"].startswith("<b>ok</b>")
    assert "onerror" not in sent["formatted_body"] and "src" not in sent["formatted_body"]
    assert sent["body"] == "ok"
