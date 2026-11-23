from __future__ import annotations
from contextlib import asynccontextmanager
from html.parser import HTMLParser
import logging
import unicodedata
import nh3
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from mautrix.api import Method, Path
from mautrix.errors import MatrixRequestError
from mautrix.types import EventID, EventType, RoomAlias, RoomID

from crypto import get_bot, start_bot, stop_bot

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await start_bot()
    try:
        yield
    finally:
        await stop_bot()


app = FastAPI(lifespan=lifespan)


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
        if tag in ("td", "th"):
            self.parts.append(" ")
        elif tag in self.BLOCK:
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


TITLE_MAX = 60
PLACEHOLDER = "ここから会話を始めます。"


def _is_regional(c: str) -> bool:
    return 0x1F1E6 <= ord(c) <= 0x1F1FF


def _attaches(c: str) -> bool:
    return (unicodedata.category(c) in ("Mn", "Me") or c in "\u200d\ufe0e\ufe0f\u20e3"
            or 0x1F3FB <= ord(c) <= 0x1F3FF or 0xE0020 <= ord(c) <= 0xE007F)


def cut_title(line: str) -> tuple[str, bool]:
    if len(line) <= TITLE_MAX:
        return line, False
    i = TITLE_MAX - 1
    while i > 0 and (_attaches(line[i]) or line[i - 1] == "\u200d"):
        i -= 1
    run = 0
    while run < i and _is_regional(line[i - 1 - run]):
        run += 1
    if run % 2 and _is_regional(line[i]):
        i -= 1
    if i <= 0:
        i = TITLE_MAX - 1
    return line[:i].rstrip() + "…", True


def split_post(text: str, title: str, html: bool) -> tuple[str, str]:
    plain = html_to_text(sanitize_html(text)) if html else text
    lines = plain.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if title:
        return title, PLACEHOLDER if first is None else text
    if first is None:
        raise HTTPException(400, "`text` has no visible text")
    root, cut = cut_title(" ".join(lines[first].split()))
    rest = "\n".join(lines[first + 1:]).strip("\n")
    if html or cut or not rest.strip():
        return root, text
    return root, rest


def thread_relation(root_id: str) -> dict:
    return {
        "rel_type": "m.thread",
        "event_id": root_id,
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": root_id},
    }


def matrix_error(e: MatrixRequestError) -> HTTPException:
    return HTTPException(e.http_status if e.http_status in (403, 404) else 500, f"Matrix error: {e}")


async def resolve_room(target: str) -> RoomID:
    if target.startswith("#"):
        return (await get_bot().client.resolve_room_alias(RoomAlias(target))).room_id
    return RoomID(target)


async def send(room_id: RoomID, content: dict) -> str:
    return await get_bot().client.send_message_event(room_id, EventType.ROOM_MESSAGE, content)


async def check_thread_root(room_id: RoomID, event_id: str) -> None:
    try:
        evt = await get_bot().client.api.request(
            Method.GET, Path.v3.rooms[room_id].event[EventID(event_id)])
    except MatrixRequestError as e:
        if e.http_status == 404:
            raise HTTPException(404, "`thread_root` was not found in this room")
        raise matrix_error(e)
    if (evt.get("type") not in ("m.room.message", "m.room.encrypted") or "state_key" in evt
            or "redacted_because" in evt.get("unsigned", {})):
        raise HTTPException(400, "`thread_root` is not a message that can start a thread")
    if "m.relates_to" in evt.get("content", {}):
        raise HTTPException(400, "`thread_root` is a reply or relation, not a thread root")


def _field(p: dict, name: str, kind: type, default):
    value = p.get(name)
    if value is None:
        return default
    if not isinstance(value, kind):
        raise HTTPException(400, f"`{name}` must be a {kind.__name__}")
    return value


@app.exception_handler(StarletteHTTPException)
async def http_error(_req: Request, e: StarletteHTTPException):
    return JSONResponse({"ok": False, "error": e.detail}, status_code=e.status_code)


# ─── Webhook endpoint ────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(req: Request):
    try:
        p = await req.json()
    except ValueError:
        raise HTTPException(400, "The body must be a JSON object")
    if not isinstance(p, dict):
        raise HTTPException(400, "The body must be a JSON object")

    text = _field(p, "text", str, "")
    title = cut_title(" ".join(_field(p, "title", str, "").split()))[0]
    thread = _field(p, "thread", bool, True)
    root_id = _field(p, "thread_root", str, None)
    ts = p.get("thread_ts")
    ts = ts if isinstance(ts, str) and ts.startswith("$") else None
    if root_id and ts and root_id != ts:
        raise HTTPException(400, "`thread_root` and `thread_ts` differ")
    root_id = root_id or ts
    if root_id and not thread:
        raise HTTPException(400, "`thread_root` cannot be used with `thread: false`")
    html = p.get("format") == "html"

    if not text.strip() and not (title and thread and not root_id):
        raise HTTPException(400, "`text` field is required")

    target = _field(p, "channel", str, None) or _field(p, "room_id", str, None)
    if not target:
        raise HTTPException(400, "`channel` or `room_id` is required")

    try:
        room_id = await resolve_room(target)
    except MatrixRequestError as e:
        raise matrix_error(e)
    try:
        await get_bot().client.join_room(room_id, max_retries=0)
    except MatrixRequestError as e:
        if e.http_status in (403, 404):
            raise matrix_error(e)
        log.warning("Could not join %s, sending anyway: %s", room_id, e)

    new_root = None
    if root_id:
        await check_thread_root(room_id, root_id)
    elif thread:
        root, text = split_post(text, title, html)
        try:
            new_root = root_id = await send(room_id, {"msgtype": "m.text", "body": root, "m.mentions": {}})
        except MatrixRequestError as e:
            raise matrix_error(e)

    content = build_content(text, html)
    if root_id:
        content["m.relates_to"] = thread_relation(root_id)
    try:
        eid = await send(room_id, content)
    except Exception as e:
        if not new_root:
            if isinstance(e, MatrixRequestError):
                raise matrix_error(e)
            log.error("Could not send to %s: %r", room_id, e)
            raise HTTPException(500, f"The message could not be sent: {e}")
        log.error("Sent thread root %s in %s but not its body: %r", new_root, room_id, e)
        try:
            await get_bot().client.redact(room_id, new_root, reason="The thread body could not be sent")
        except Exception as redact_error:
            log.error("Could not redact thread root %s: %s", new_root, redact_error)
        raise HTTPException(500, f"Thread not created, the body could not be sent: {e}")

    return {"ok": True, "event_id": eid, "thread_root": root_id}


@app.get("/health")
async def health():
    bot = get_bot()
    return {
        "ok": bot.ready,
        "device_id": bot.client.device_id or None,
        "crypto_ready": bot.ready,
        "cross_signed": bot.cross_signed,
    }
