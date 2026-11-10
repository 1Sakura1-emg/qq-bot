import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiocqhttp import Event

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

spec = importlib.util.spec_from_file_location(
    "jinli_friends_test",
    ROOT / "plugins/astrbot_plugin_jinli_friends/main.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FriendApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = AiocqhttpAdapter(
            {
                "id": "jinli-personal",
                "ws_reverse_host": "127.0.0.1",
                "ws_reverse_port": 6199,
                "ws_reverse_token": "test-only",
            },
            {},
            asyncio.Queue(),
        )
        self.bot = SimpleNamespace(call_action=AsyncMock())
        self.plugin = module.JinliFriends(
            SimpleNamespace(),
            {
                "enabled": True,
                "platform_id": "jinli-personal",
                "account_id": "10000",
            },
        )

    async def make_event(self, **overrides):
        raw = {
            "time": 0,
            "self_id": 10000,
            "post_type": "request",
            "request_type": "friend",
            "user_id": 10001,
            "comment": "test",
            "flag": "test-flag",
        }
        raw.update(overrides)
        message = await self.adapter.convert_message(Event(raw))
        return AiocqhttpMessageEvent(
            "", message, self.adapter.meta(), message.session_id, self.bot
        )

    async def test_approval_and_duplicate_delivery(self):
        first = await self.make_event()
        second = await self.make_event()
        await asyncio.gather(
            self.plugin.accept_friend_request(first),
            self.plugin.accept_friend_request(second),
        )
        self.bot.call_action.assert_awaited_once_with(
            "set_friend_add_request", flag="test-flag", approve=True
        )
        self.assertTrue(first.is_stopped())
        self.assertTrue(second.is_stopped())

    async def test_other_account_and_disabled_are_ignored(self):
        other = await self.make_event(self_id=10002)
        await self.plugin.accept_friend_request(other)
        self.assertFalse(other.is_stopped())
        self.plugin.config["enabled"] = False
        disabled = await self.make_event()
        await self.plugin.accept_friend_request(disabled)
        self.assertFalse(disabled.is_stopped())
        self.bot.call_action.assert_not_awaited()

    async def test_other_platform_is_ignored(self):
        event = await self.make_event()
        event.platform_meta = SimpleNamespace(id="another-platform")
        await self.plugin.accept_friend_request(event)
        self.bot.call_action.assert_not_awaited()

    async def test_chat_notice_and_group_invitation_are_ignored(self):
        for overrides in (
            {"post_type": "notice", "notice_type": "friend_add"},
            {"request_type": "group", "group_id": 10003, "sub_type": "invite"},
        ):
            event = await self.make_event(**overrides)
            await self.plugin.accept_friend_request(event)
            self.assertFalse(event.is_stopped())
        chat = await self.make_event()
        chat.message_obj.raw_message = {"post_type": "message", "message": "hello"}
        await self.plugin.accept_friend_request(chat)
        self.assertFalse(chat.is_stopped())
        self.bot.call_action.assert_not_awaited()

    async def test_missing_flag_does_not_call_api(self):
        event = await self.make_event(flag="")
        await self.plugin.accept_friend_request(event)
        self.assertTrue(event.is_stopped())
        self.bot.call_action.assert_not_awaited()

    async def test_failed_approval_can_be_retried_without_chat_reply(self):
        self.bot.call_action.side_effect = RuntimeError("offline")
        failed = await self.make_event()
        await self.plugin.accept_friend_request(failed)
        self.assertTrue(failed.is_stopped())
        self.bot.call_action.side_effect = None
        await self.plugin.accept_friend_request(await self.make_event())
        self.assertEqual(self.bot.call_action.await_count, 2)


if __name__ == "__main__":
    unittest.main()
