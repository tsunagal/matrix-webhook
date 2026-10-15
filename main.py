from __future__ import annotations
import os, uuid, urllib.parse, requests, json
from html.parser import HTMLParser
import nh3
from fastapi import FastAPI, Request, HTTPException

# ─── Config ──────────────────────────────────────────────────────────
MX      = os.getenv("MATRIX_URL", "http://localhost:8008").rstrip("/")
AS_TOK  = os.environ["AS_TOKEN"]
LOCAL   = os.getenv("BOT_LOCALPART", "webhook_bot")
SERVER  = os.getenv("SERVER_NAME",   "localhost")
USER_ID = f"@{LOCAL}:{SERVER}"

app = FastAPI()


# ─── Helper for HTTP calls ───────────────────────────────────────────
def mx(method: str, path: str, **kw) -> requests.Response:
    return requests.request(method, f"{MX}{path}", timeout=5, **kw)


# ─── Ensure the ghost user exists ────────────────────────────────────
def ensure_registered() -> None:
    payload = {"username": LOCAL, "type": "m.login.application_service"}
    r = mx("POST",
           "/_matrix/client/v3/register",
           params={"access_token": AS_TOK},
           json=payload)
    if r.status_code == 200:
        return                                # success
    if r.status_code == 400:                  # could be already registered
        try:
            err = r.json().get("errcode", "")
        except json.JSONDecodeError:
            err = ""
        if err in ("M_USER_IN_USE", "M_EXCLUSIVE"):
            return                            # already exists → fine
    r.raise_for_status()


# ─── Matrix primitives ───────────────────────────────────────────────
def alias_to_room_id(alias: str) -> str:
    enc = urllib.parse.quote(alias)
    r = mx("GET",
           f"/_matrix/client/v3/directory/room/{enc}",
           params={"access_token": AS_TOK})
    r.raise_for_status()
    return r.json()["room_id"]


def ensure_join(room: str) -> None:
    enc = urllib.parse.quote(room)
    mx("POST",
       f"/_matrix/client/v3/join/{enc}",
       params={"access_token": AS_TOK, "user_id": USER_ID})


# ─── HTML sanitising (Matrix spec: m.room.message formatted_body) ────
ALLOWED_TAGS = {
    "font", "del", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "p",
    "a", "ul", "ol", "sup", "sub", "li", "b", "i", "u", "strong", "em", "s",
    "code", "hr", "br", "div", "table", "thead", "tbody", "tr", "th", "td",
    "caption", "pre", "span", "img", "details", "summary",
}
ALLOWED_ATTRS = {
    "font": {"data-mx-bg-color", "data-mx-color", "color"},
    "span": {"data-mx-bg-color", "data-mx-color", "data-mx-spoiler", "data-mx-maths"},
    "div":  {"data-mx-maths"},
    "a":    {"href", "target"},
    "img":  {"width", "height", "alt", "title", "src"},
    "ol":   {"start"},
    "code": {"class"},
}
URL_SCHEMES = {"https", "http", "ftp", "mailto", "magnet", "mxc"}


def _filter_attr(tag: str, attr: str, value: str) -> str | None:
    if tag == "img" and attr == "src" and not value.startswith("mxc://"):
        return None
    if tag == "code" and attr == "class" and not value.startswith("language-"):
        return None
    return value


def sanitize_html(text: str) -> str:
    return nh3.clean(text, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS,
                     url_schemes=URL_SCHEMES, attribute_filter=_filter_attr,
                     link_rel=None)


class _PlainText(HTMLParser):
    BLOCK = {"p", "div", "ul", "ol", "tr", "blockquote", "pre",
             "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "img":
            self.parts.append(dict(attrs).get("alt") or "")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(text: str) -> str:
    parser = _PlainText()
    parser.feed(text)
    parser.close()
    lines = [line.rstrip() for line in "".join(parser.parts).split("\n")]
    return "\n".join(lines).strip("\n")


def build_content(text: str, html: bool) -> dict:
    if not html:
        return {"msgtype": "m.text", "body": text}
    formatted = sanitize_html(text)
    return {
        "msgtype": "m.text",
        "body": html_to_text(formatted),
        "format": "org.matrix.custom.html",
        "formatted_body": formatted,
    }


def send(room_id: str, body: str, html: bool) -> str:
    txn  = uuid.uuid4().hex
    content = build_content(body, html)
    r = mx("PUT",
           f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
           params={"access_token": AS_TOK, "user_id": USER_ID},
           json=content)
    r.raise_for_status()
    return r.json()["event_id"]


# ─── Webhook endpoint ────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(req: Request):
    p = await req.json()

    text = p.get("text")
    if not text:
        raise HTTPException(400, "`text` field is required")

    target = p.get("channel") or p.get("room_id")
    if not target:
        raise HTTPException(400, "`channel` or `room_id` is required")

    # make sure bot exists
    ensure_registered()

    # alias → room-ID
    room_id = alias_to_room_id(target) if target.startswith("#") else target

    # join (idempotent)
    try:
        ensure_join(room_id)
    except Exception:
        pass                                   # invite-only room w/out invite

    #  send
    try:
        eid = send(room_id, text, p.get("format") == "html")
    except requests.HTTPError as e:
        raise HTTPException(500, f"Matrix error: {e.response.text}")

    return {"ok": True, "event_id": eid}