from __future__ import annotations
from contextlib import asynccontextmanager
from html.parser import HTMLParser
import logging
import nh3
from fastapi import FastAPI, Request, HTTPException
from mautrix.errors import MatrixRequestError
from mautrix.types import EventType, RoomAlias, RoomID

from crypto import get_bot, start_bot, stop_bot

logging.basicConfig(level=logging.INFO)


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


async def resolve_room(target: str) -> RoomID:
    if target.startswith("#"):
        return (await get_bot().client.resolve_room_alias(RoomAlias(target))).room_id
    return RoomID(target)


async def send(room_id: RoomID, body: str, html: bool) -> str:
    return await get_bot().client.send_message_event(
        room_id, EventType.ROOM_MESSAGE, build_content(body, html)
    )


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

    try:
        room_id = await resolve_room(target)
    except MatrixRequestError as e:
        raise HTTPException(500, f"Matrix error: {e}")

    # join (idempotent)
    try:
        await get_bot().client.join_room(room_id, max_retries=0)
    except MatrixRequestError:
        pass                                   # invite-only room w/out invite

    try:
        eid = await send(room_id, text, p.get("format") == "html")
    except MatrixRequestError as e:
        raise HTTPException(500, f"Matrix error: {e}")

    return {"ok": True, "event_id": eid}


@app.get("/health")
async def health():
    bot = get_bot()
    return {
        "ok": bot.ready,
        "device_id": bot.client.device_id or None,
        "crypto_ready": bot.ready,
        "cross_signed": bot.cross_signed,
    }
