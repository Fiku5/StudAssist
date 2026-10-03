import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from aiogram.types import Message, CallbackQuery, User

from bot.middlewares import AuthMiddleware
from config import get_allowed_user_ids
import config


def test_auth_middleware_defaults_to_single_owner(monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "")
    assert get_allowed_user_ids() == [1017867274]

    middleware = AuthMiddleware()
    handler = AsyncMock(return_value="handler_called")

    # Authorized user 1017867274
    msg_owner = MagicMock(spec=Message)
    msg_owner.from_user = User(id=1017867274, is_bot=False, first_name="Owner")
    msg_owner.text = "/start"

    res_owner = asyncio.run(middleware(handler, msg_owner, {}))
    assert res_owner == "handler_called"
    handler.assert_awaited_once()

    # Unauthorized user 999999
    handler.reset_mock()
    msg_stranger = MagicMock(spec=Message)
    msg_stranger.from_user = User(id=999999, is_bot=False, first_name="Stranger")
    msg_stranger.text = "/start"
    msg_stranger.answer = AsyncMock()

    res_stranger = asyncio.run(middleware(handler, msg_stranger, {}))
    assert res_stranger is None
    handler.assert_not_awaited()
    msg_stranger.answer.assert_not_awaited()


def test_auth_middleware_blocks_unauthorized_user_silently(monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "1017867274")

    middleware = AuthMiddleware()
    handler = AsyncMock(return_value="handler_called")

    # Mock message from unauthorized user
    msg = MagicMock(spec=Message)
    msg.from_user = User(id=999999, is_bot=False, first_name="Stranger")
    msg.text = "/sessions"
    msg.answer = AsyncMock()

    res = asyncio.run(middleware(handler, msg, {}))
    assert res is None
    handler.assert_not_awaited()
    # Ensure bot did NOT reply or react
    msg.answer.assert_not_awaited()


def test_auth_middleware_allows_authorized_user(monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "1017867274")

    middleware = AuthMiddleware()
    handler = AsyncMock(return_value="handler_called")

    # Mock message from authorized user
    msg = MagicMock(spec=Message)
    msg.from_user = User(id=1017867274, is_bot=False, first_name="Owner")
    msg.text = "/schedule"
    msg.answer = AsyncMock()

    res = asyncio.run(middleware(handler, msg, {}))
    assert res == "handler_called"
    handler.assert_awaited_once()


def test_auth_middleware_blocks_myid_for_unauthorized_silently(monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "1017867274")

    middleware = AuthMiddleware()
    handler = AsyncMock(return_value="myid_handler_called")

    # Mock /myid message from unauthorized user
    msg = MagicMock(spec=Message)
    msg.from_user = User(id=999999, is_bot=False, first_name="Stranger")
    msg.text = "/myid"
    msg.answer = AsyncMock()

    res = asyncio.run(middleware(handler, msg, {}))
    assert res is None
    handler.assert_not_awaited()
    msg.answer.assert_not_awaited()


def test_auth_middleware_blocks_callback_from_unauthorized_silently(monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "1017867274")

    middleware = AuthMiddleware()
    handler = AsyncMock(return_value="handler_called")

    # Mock callback from unauthorized user
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = User(id=999999, is_bot=False, first_name="Stranger")
    cb.answer = AsyncMock()

    res = asyncio.run(middleware(handler, cb, {}))
    assert res is None
    handler.assert_not_awaited()
    cb.answer.assert_not_awaited()


def test_auth_middleware_silently_drops_group_and_channel_messages(monkeypatch):
    """Verifies that all events from groups, supergroups, and channels are silently ignored."""
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "1017867274")
    middleware = AuthMiddleware()
    handler = AsyncMock(return_value="handler_called")

    for non_private_type in ["group", "supergroup", "channel"]:
        # Message from authorized user, but in a non-private chat (e.g. subject topic)
        msg = MagicMock(spec=Message)
        msg.from_user = User(id=1017867274, is_bot=False, first_name="Owner")
        msg.chat = MagicMock()
        msg.chat.type = non_private_type
        msg.text = "Here is a file or message in group"
        msg.answer = AsyncMock()

        res = asyncio.run(middleware(handler, msg, {}))
        assert res is None
        handler.assert_not_awaited()
        msg.answer.assert_not_awaited()

        # Callback query from non-private chat
        cb = MagicMock(spec=CallbackQuery)
        cb.from_user = User(id=1017867274, is_bot=False, first_name="Owner")
        cb.message = MagicMock(spec=Message)
        cb.message.chat = MagicMock()
        cb.message.chat.type = non_private_type
        cb.answer = AsyncMock()

        res_cb = asyncio.run(middleware(handler, cb, {}))
        assert res_cb is None
        handler.assert_not_awaited()
        cb.answer.assert_not_awaited()

