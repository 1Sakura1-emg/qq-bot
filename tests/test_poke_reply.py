import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiocqhttp import Event

from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

spec = importlib.util.spec_from_file_location(
    "jinli_poke_test", ROOT / "plugins/astrbot_plugin_jinli_poke/main.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PokeReplyTests(unittest.IsolatedAsyncioTestCase):
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
        self.plugin = module.JinliPoke(
            SimpleNamespace(),
            {
                "enabled": True,
                "platform_id": "jinli-personal",
                "account_id": "10000",
            },
        )

    async def make_event(self, group=20001, target=10000, sender=10001):
        raw = {
            "time": 0,
            "self_id": 10000,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "user_id": sender,
            "target_id": target,
        }
        if group:
            raw["group_id"] = group
        converted = await self.adapter.convert_message(Event(raw))
        return AiocqhttpMessageEvent(
            converted.message_str,
            converted,
            self.adapter.meta(),
            converted.session_id,
            SimpleNamespace(),
        )

    async def test_group_poke_reaches_existing_agent_pipeline(self):
        event = await self.make_event()
        await self.plugin.respond_to_poke(event)
        received = []

        async def run_agent(incoming):
            received.append(incoming)
            yield

        stage = ProcessStage()
        stage.ctx = SimpleNamespace(
            astrbot_config={"provider_settings": {"enable": True}}
        )
        stage.agent_sub_stage = SimpleNamespace(process=run_agent)
        async for _ in stage.process(event):
            pass
        self.assertEqual(received, [event])
        self.assertEqual(event.get_group_id(), "20001")
        self.assertIn("戳了戳你", event.message_str)
        self.assertIn("烬璃", event.message_str)
        self.assertFalse(event.is_stopped())

    async def test_private_poke_keeps_private_routing(self):
        event = await self.make_event(group=None)
        await self.plugin.respond_to_poke(event)
        self.assertTrue(event.is_private_chat())
        self.assertEqual(event.get_sender_id(), "10001")
        self.assertTrue(event.is_at_or_wake_command)

    async def test_poking_someone_else_or_self_does_not_reply(self):
        for kwargs in [{"target": 99999}, {"sender": 10000}]:
            event = await self.make_event(**kwargs)
            await self.plugin.respond_to_poke(event)
            self.assertTrue(event.is_stopped())
            self.assertEqual(event.message_str, "")

    async def test_repeated_pokes_share_group_cooldown(self):
        with patch.object(module.time, "monotonic", side_effect=[100, 101, 116]):
            first = await self.make_event()
            second = await self.make_event(sender=10002)
            later = await self.make_event()
            for event in [first, second, later]:
                await self.plugin.respond_to_poke(event)
        self.assertFalse(first.is_stopped())
        self.assertTrue(second.is_stopped())
        self.assertFalse(later.is_stopped())

    async def test_other_groups_have_separate_cooldown(self):
        first = await self.make_event(group=20001)
        other = await self.make_event(group=20002)
        await self.plugin.respond_to_poke(first)
        await self.plugin.respond_to_poke(other)
        self.assertTrue(first.is_at_or_wake_command)
        self.assertTrue(other.is_at_or_wake_command)

    async def test_disabled_or_other_account_does_not_modify_event(self):
        event = await self.make_event()
        self.plugin.config["enabled"] = False
        await self.plugin.respond_to_poke(event)
        self.plugin.config["enabled"] = True
        event.message_obj.self_id = "99999"
        await self.plugin.respond_to_poke(event)
        self.assertEqual(event.message_str, "")
        self.assertFalse(event.is_at_or_wake_command)

    async def test_plain_messages_and_other_notices_are_unchanged(self):
        for post_type, sub_type in [("message", "normal"), ("notice", "other")]:
            event = await self.make_event()
            event.message_obj.raw_message["post_type"] = post_type
            event.message_obj.raw_message["sub_type"] = sub_type
            event.message_str = "unchanged"
            await self.plugin.respond_to_poke(event)
            self.assertEqual(event.message_str, "unchanged")
            self.assertFalse(event.is_stopped())


if __name__ == "__main__":
    unittest.main()
