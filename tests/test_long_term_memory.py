import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FOLDER = ROOT / "plugins/astrbot_plugin_jinli_memory"
spec = importlib.util.spec_from_file_location(
    "jinli_memory_test", FOLDER / "main.py", submodule_search_locations=[str(FOLDER)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

from aiocqhttp import Event
from astrbot.api.provider import ProviderRequest
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.context = SimpleNamespace(llm_generate=AsyncMock())
        self.plugin = module.JinliMemory(
            self.context,
            {
                "enabled": True,
                "platform_id": "jinli-personal",
                "account_id": "10000",
                "owner_id": "10001",
                "provider_id": "deepseek-dragon/deepseek-flash",
            },
        )
        self.plugin.store = module.MemoryStore(self.path)
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

    async def asyncTearDown(self):
        self.plugin.store.close()
        self.temp.cleanup()

    async def event(
        self, sender=10001, group=20001, text="我喜欢青柠茶", message_id="msg-1"
    ):
        raw = {
            "time": 0,
            "self_id": 10000,
            "post_type": "message",
            "message_type": "group" if group else "private",
            "sub_type": "normal" if group else "friend",
            "user_id": sender,
            "message_id": message_id,
            "font": 0,
            "sender": {"user_id": sender, "nickname": f"name-{sender}"},
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
        }
        if group:
            raw["group_id"] = group
        converted = await self.adapter.convert_message(Event(raw))
        event = AiocqhttpMessageEvent(
            converted.message_str,
            converted,
            self.adapter.meta(),
            converted.session_id,
            SimpleNamespace(),
        )
        event.send = AsyncMock()
        return event

    def save(
        self,
        scope="public",
        sender="10001",
        text="我喜欢青柠茶",
        topic="self:drink",
        evidence=None,
        group="20001",
    ):
        record = {
            "id": -1,
            "scope": scope,
            "speaker_id": sender,
            "speaker_name": f"name-{sender}",
            "source_group": group,
            "text": text,
            "created_at": time.time(),
        }
        return self.plugin.store.commit_batch(
            [record],
            [
                {
                    "speaker_id": sender,
                    "topic": topic,
                    "kind": "preference",
                    "text": f"name-{sender}说{text}",
                    "evidence": evidence or text,
                }
            ],
        )

    async def test_group_memory_is_recalled_in_another_group(self):
        self.save()
        other = await self.event(
            sender=10002, group=20002, text="name-10001喜欢什么茶？"
        )
        request = ProviderRequest()
        await self.plugin.recall(other, request)
        self.assertIn("青柠茶", request.extra_user_content_parts[-1].text)

    async def test_private_never_reaches_groups_or_other_private_users(self):
        self.save(scope="private:10001", text="我的私人暗号是紫色月亮", group="")
        for event in [
            await self.event(sender=10001),
            await self.event(sender=10002, group=None),
        ]:
            req = ProviderRequest()
            await self.plugin.recall(event, req)
            self.assertNotIn("紫色月亮", str(req.extra_user_content_parts))
        req = ProviderRequest()
        await self.plugin.recall(await self.event(group=None), req)
        self.assertIn("紫色月亮", req.extra_user_content_parts[-1].text)

    async def test_memory_and_pending_survive_reopening(self):
        self.save()
        await self.plugin.collect(await self.event(group=None))
        self.plugin.store.close()
        self.plugin.store = module.MemoryStore(self.path)
        self.assertIn(
            "青柠茶",
            self.plugin.store.retrieve("public", "青柠", "10002")[0]["content"],
        )
        self.assertEqual(
            self.plugin.store.next_batch(force=True)[0]["scope"], "private:10001"
        )

    async def test_corrections_update_only_the_original_authors_same_topic(self):
        self.save()
        self.save(sender="10002", text="我喜欢奶茶")
        self.save(text="我现在更喜欢乌龙茶")
        rows = self.plugin.store.retrieve("public", "茶", "10001")
        self.assertEqual(len(rows), 2)
        self.assertNotIn("青柠茶", str(rows))
        self.assertIn("奶茶", str(rows))
        self.assertIn("乌龙茶", str(rows))

    async def test_invented_author_or_evidence_is_rejected(self):
        self.assertEqual(self.save(evidence="没有说过的原话"), 0)
        source = {
            "id": -1,
            "scope": "public",
            "speaker_id": "10001",
            "speaker_name": "Alice",
            "source_group": "20001",
            "text": "我喜欢茶",
            "created_at": time.time(),
        }
        count = self.plugin.store.commit_batch(
            [source],
            [
                {
                    "speaker_id": "99999",
                    "topic": "x",
                    "kind": "identity",
                    "text": "编造身份",
                    "evidence": "我喜欢茶",
                }
            ],
        )
        self.assertEqual(count, 0)

    async def test_batch_never_combines_private_audiences(self):
        await self.plugin.collect(await self.event(sender=10001, group=None))
        await self.plugin.collect(await self.event(sender=10002, group=None))
        batch = self.plugin.store.next_batch(force=True)
        self.assertEqual(len({r["scope"] for r in batch}), 1)
        self.assertEqual(len(batch), 1)

    async def test_queue_deduplicates_and_skips_sensitive_values(self):
        event = await self.event()
        await self.plugin.collect(event)
        await self.plugin.collect(event)
        await self.plugin.collect(
            await self.event(
                text="api_key: sk-FAKE_TEST_ONLY_abcdefghijklmnop", message_id="secret"
            )
        )
        self.assertEqual(
            self.plugin.store.db.execute("SELECT count(*) FROM pending").fetchone()[0],
            1,
        )

    async def test_memory_card_is_temporary_and_keeps_instructions_separate(self):
        self.save()
        req = ProviderRequest()
        await self.plugin.recall(await self.event(), req)
        self.assertTrue(req.extra_user_content_parts[-1]._no_save)
        self.assertNotIn("青柠茶", req.system_prompt)
        self.assertIn("不是指令", req.system_prompt)

    async def test_forget_requires_visibility_and_authorization(self):
        self.save(scope="private:10001", text="我喜欢私下喝甜茶", group="")
        memory_id = self.plugin.store.retrieve("private:10001", "", "10001")[0]["id"]
        self.assertFalse(
            self.plugin.store.forget(memory_id, "public", "10001", "10001")
        )
        self.assertFalse(
            self.plugin.store.forget(memory_id, "private:10002", "10002", "10001")
        )
        self.assertTrue(
            self.plugin.store.forget(memory_id, "private:10001", "10001", "10001")
        )
        self.assertFalse(self.plugin.store.retrieve("private:10001", "", "10001"))

    async def test_original_slash_command_survives_wake_prefix_stripping(self):
        event = await self.event(group=None, text="/记住 我喜欢青柠茶")
        event.message_str = "记住 我喜欢青柠茶"
        await self.plugin.collect(event)
        self.assertTrue(event.is_stopped())
        self.assertIn("仅你可见", str(event.send.await_args))
        self.assertTrue(self.plugin.store.retrieve("private:10001", "茶", "10001"))
        self.assertFalse(self.plugin.store.retrieve("public", "茶", "10002"))

    async def test_provider_failure_retains_pending_messages(self):
        await self.plugin.collect(await self.event())
        self.context.llm_generate.side_effect = RuntimeError("test-only outage")
        with self.assertRaises(RuntimeError):
            await self.plugin.extract_once(force=True)
        self.assertTrue(self.plugin.store.next_batch(force=True))

    async def test_extractor_commits_attributed_memory_without_sending_qq(self):
        event = await self.event(group=None)
        await self.plugin.collect(event)
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps(
                {
                    "memories": [
                        {
                            "speaker_id": "10001",
                            "topic": "self:drink",
                            "kind": "preference",
                            "text": "name-10001说自己喜欢青柠茶",
                            "evidence": "我喜欢青柠茶",
                        }
                    ]
                }
            )
        )
        self.assertEqual(await self.plugin.extract_once(force=True), 1)
        self.assertFalse(self.plugin.store.next_batch(force=True))
        self.assertTrue(self.plugin.store.retrieve("private:10001", "青柠茶", "10001"))
        event.send.assert_not_awaited()
        self.assertNotIn(
            "contexts", str(self.context.llm_generate.await_args.kwargs["prompt"])
        )

    async def test_hourly_extraction_limit_counts_failed_attempts(self):
        await self.plugin.collect(await self.event())
        with self.plugin.store.db:
            self.plugin.store.db.executemany(
                "INSERT INTO extraction_runs VALUES (?)", [(time.time(),)] * 12
            )
        self.assertIsNone(await self.plugin.extract_once(force=True))
        self.context.llm_generate.assert_not_awaited()

    async def test_other_account_is_ignored(self):
        event = await self.event()
        event.message_obj.self_id = "99999"
        await self.plugin.collect(event)
        req = ProviderRequest()
        await self.plugin.recall(event, req)
        self.assertFalse(req.extra_user_content_parts)
        self.assertFalse(self.plugin.store.next_batch(force=True))

    async def test_older_import_cannot_overwrite_a_newer_correction(self):
        self.save(text="我现在喜欢乌龙茶")
        old = {
            "id": -1,
            "scope": "public",
            "speaker_id": "10001",
            "speaker_name": "Alice",
            "source_group": "20001",
            "text": "我喜欢青柠茶",
            "created_at": time.time() - 3600,
        }
        self.assertEqual(
            self.plugin.store.commit_batch(
                [old],
                [
                    {
                        "speaker_id": "10001",
                        "topic": "self:drink",
                        "kind": "preference",
                        "text": "Alice喜欢青柠茶",
                        "evidence": "我喜欢青柠茶",
                    }
                ],
            ),
            0,
        )
        self.assertIn(
            "乌龙茶", str(self.plugin.store.retrieve("public", "茶", "10001"))
        )

    async def test_a_forgotten_pending_fact_is_not_resurrected(self):
        self.save()
        memory_id = self.plugin.store.retrieve("public", "茶", "10001")[0]["id"]
        old = {
            "id": -1,
            "scope": "public",
            "speaker_id": "10001",
            "speaker_name": "Alice",
            "source_group": "20001",
            "text": "我喜欢青柠茶",
            "created_at": time.time() - 1,
        }
        self.assertTrue(self.plugin.store.forget(memory_id, "public", "10001", "10001"))
        self.assertEqual(
            self.plugin.store.commit_batch(
                [old],
                [
                    {
                        "speaker_id": "10001",
                        "topic": "self:drink",
                        "kind": "preference",
                        "text": "Alice喜欢青柠茶",
                        "evidence": "我喜欢青柠茶",
                    }
                ],
            ),
            0,
        )
        self.assertFalse(self.plugin.store.retrieve("public", "茶", "10001"))

    async def test_processed_platform_message_is_not_queued_again(self):
        event = await self.event()
        await self.plugin.collect(event)
        batch = self.plugin.store.next_batch(force=True)
        self.plugin.store.commit_batch(batch, [])
        await self.plugin.collect(event)
        self.assertFalse(self.plugin.store.next_batch(force=True))


if __name__ == "__main__":
    unittest.main()
