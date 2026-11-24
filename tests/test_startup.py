import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace

os.environ.setdefault("AS_TOKEN", "test")

import pytest  # noqa: E402
from mautrix.errors import MatrixUnknownRequestError  # noqa: E402

import crypto  # noqa: E402


def make_bot(result):
    async def request(method, path, *args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    bot = crypto.Bot.__new__(crypto.Bot)
    bot.client = SimpleNamespace(api=SimpleNamespace(request=request))
    return bot


def error(errcode, status=400):
    return MatrixUnknownRequestError(status, errcode=errcode, message=errcode)


def test_bot_outside_the_namespace_stops_startup():
    with pytest.raises(RuntimeError, match="outside the appservice's user namespace"):
        asyncio.run(make_bot(error("M_EXCLUSIVE"))._ensure_registered())


def test_existing_bot_user_is_fine():
    asyncio.run(make_bot(error("M_USER_IN_USE"))._ensure_registered())


def test_bot_as_appservice_sender_stops_startup():
    bot = make_bot({"user_id": crypto.USER_ID})
    with pytest.raises(RuntimeError, match="sender_localpart"):
        asyncio.run(bot._check_sender())


def test_other_appservice_sender_is_fine():
    asyncio.run(make_bot({"user_id": "@webhook_master:localhost"})._check_sender())


def test_failed_sender_lookup_does_not_stop_startup():
    asyncio.run(make_bot(error("M_UNRECOGNIZED", 404))._check_sender())


def test_missing_as_token_exits_with_one_clear_line():
    env = {k: v for k, v in os.environ.items() if k != "AS_TOKEN"}
    result = subprocess.run(
        [sys.executable, "-c", "import crypto"],
        env=env,
        cwd=os.path.dirname(crypto.__file__),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "AS_TOKEN is not set: put the as_token of the appservice registration in .env"
    )


def test_sender_check_runs_before_the_device_is_loaded(monkeypatch):
    calls = []
    bot = crypto.Bot.__new__(crypto.Bot)

    async def noop():
        pass

    bot.db = SimpleNamespace(start=noop)
    bot.crypto_store = SimpleNamespace(open=noop)
    monkeypatch.setattr(crypto.PgStateStore.upgrade_table, "upgrade", lambda db: noop())
    for name in ("_check_sender", "_ensure_registered", "_load_device"):
        async def step(name=name):
            calls.append(name)
        monkeypatch.setattr(bot, name, step)

    class Stop(Exception):
        pass

    async def stop_after_load():
        raise Stop

    monkeypatch.setattr(bot, "_ensure_cross_signed", stop_after_load)
    with pytest.raises(Stop):
        asyncio.run(bot.start())
    assert calls == ["_check_sender", "_ensure_registered", "_load_device"]
