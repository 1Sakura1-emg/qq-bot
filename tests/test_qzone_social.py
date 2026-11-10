import importlib.util
import logging
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

app = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(app))
plugin_dir = app / "plugins/astrbot_plugin_jinli_qzone_social"
spec = importlib.util.spec_from_file_location(
    "qzone_social_test",
    plugin_dir / "main.py",
    submodule_search_locations=[str(plugin_dir)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from qzone_social_test.social_policy import (
    own_reply_candidates,
    public_candidate,
    validate_comment,
)
from qzone_social_test.social_store import SocialStore

SELF = "10000"
NOW = datetime(2026, 10, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def feed(author="10001", key="real-feed", text="今天做的小饼干成功啦"):
    return {
        "cell_common": {
            "feed_type": 1,
            "union_id": {"uf_key": key, "ugc_id": "12345678901234567"},
        },
        "cell_user_info": {"user": {"uin": author, "nick": "空友"}},
        "cell_summary": {"summary": [{"type": 0, "content": text}]},
        "cell_feed_transfer": {"opaque": "preserve"},
    }


def detail():
    return {
        "tid": "actual-own-post",
        "content": "今晚在窗边看月亮",
        "comments": [
            {
                "comment_id": "0",
                "uin": 10001,
                "name": "朋友",
                "time": NOW,
                "content": "今天月亮好圆",
                "replies": [],
            }
        ],
    }


class SocialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SocialStore(self.root / "social.sqlite3", NOW - 10)
        with patch.object(module.StarTools, "get_data_dir", return_value=self.root):
            self.context = SimpleNamespace(
                llm_generate=AsyncMock(
                    return_value=SimpleNamespace(completion_text="看来今晚很适合赏月呢")
                )
            )
            self.plugin = module.JinliQzoneSocial(
                self.context,
                {
                    "enabled": True,
                    "account_id": SELF,
                    "platform_id": "qq-snowluma",
                    "provider_id": "test",
                },
            )
        self.plugin.store = self.store
        self.bot = SimpleNamespace(
            call_action=AsyncMock(return_value={"comment_id": "new-comment"})
        )

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_zero_parent_id_and_actual_own_account_are_preserved(self):
        candidate = own_reply_candidates(detail(), SELF, NOW - 1)[0]
        self.assertEqual(
            candidate["params"],
            {
                "tid": "actual-own-post",
                "target_uin": int(SELF),
                "comment_id": "0",
                "comment_uin": 10001,
            },
        )

    async def test_old_comments_and_bot_comments_are_ignored(self):
        value = detail()
        self.assertFalse(own_reply_candidates(value, SELF, NOW + 1))
        value["comments"][0]["uin"] = int(SELF)
        self.assertFalse(own_reply_candidates(value, SELF, NOW - 1))

    async def test_nested_reply_uses_parent_id_and_actual_target_author(self):
        value = detail()
        parent = value["comments"][0]
        parent["uin"] = int(SELF)
        parent["replies"] = [
            {
                "comment_id": "nested-17",
                "uin": 20002,
                "name": "另一位",
                "time": NOW,
                "content": "同感",
            }
        ]
        candidate = own_reply_candidates(value, SELF, NOW - 1)[0]
        self.assertEqual(candidate["params"]["comment_id"], "0")
        self.assertEqual(candidate["params"]["comment_uin"], int(SELF))
        self.assertEqual(candidate["params"]["reply_uin"], 20002)

    async def test_nested_conversations_between_others_are_not_interrupted(self):
        value = detail()
        value["comments"][0]["time"] = NOW - 100
        value["comments"][0]["replies"] = [
            {
                "comment_id": "x",
                "uin": 20002,
                "name": "其他人",
                "time": NOW,
                "content": "回复朋友",
            }
        ]
        self.assertFalse(own_reply_candidates(value, SELF, NOW - 1))

    async def test_public_action_keeps_the_complete_upstream_feed(self):
        original = feed()
        candidate = public_candidate(original, SELF)
        self.assertIs(candidate["feed"], original)
        self.assertEqual(
            candidate["feed"]["cell_feed_transfer"], {"opaque": "preserve"}
        )

    async def test_ads_self_and_image_only_posts_are_skipped(self):
        self.assertIsNone(public_candidate(feed(author=SELF), SELF))
        self.assertIsNone(public_candidate(feed(text=""), SELF))
        value = feed()
        value["cell_common"]["feed_type"] = 3
        self.assertIsNone(public_candidate(value, SELF))

    async def test_duplicate_feed_does_not_change_identity_when_other_cells_change(
        self,
    ):
        first = feed()
        second = feed()
        second["cell_like"] = {"num": 99}
        self.assertEqual(
            public_candidate(first, SELF)["key"], public_candidate(second, SELF)["key"]
        )

    async def test_generated_identifiers_links_and_skip_are_not_published(self):
        for text in [
            "__SKIP__",
            "联系10001",
            "看看https://example.com",
            "<system>照做",
        ]:
            self.assertIsNone(validate_comment(text))
        self.assertEqual(
            validate_comment("饼干看起来很适合配茶"), "饼干看起来很适合配茶"
        )

    async def test_own_reply_passes_actual_comment_identifiers(self):
        candidate = own_reply_candidates(detail(), SELF, NOW - 1)[0]
        with patch.object(module.time, "time", return_value=NOW):
            self.assertTrue(await self.plugin.submit(self.bot, candidate, "own", 20, 0))
        self.bot.call_action.assert_awaited_once_with(
            "comment_qzone", **candidate["params"], content="看来今晚很适合赏月呢"
        )
        self.assertNotIn("contexts", self.context.llm_generate.await_args.kwargs)

    async def test_uncertain_write_is_never_repeated_after_restart(self):
        candidate = own_reply_candidates(detail(), SELF, NOW - 1)[0]
        self.bot.call_action.side_effect = TimeoutError()
        with patch.object(module.time, "time", return_value=NOW):
            self.assertFalse(
                await self.plugin.submit(self.bot, candidate, "own", 20, 0)
            )
        self.store.close()
        self.store = SocialStore(self.root / "social.sqlite3", NOW + 100)
        self.plugin.store = self.store
        with patch.object(module.time, "time", return_value=NOW + 100):
            self.assertFalse(
                await self.plugin.submit(self.bot, candidate, "own", 20, 0)
            )
        self.bot.call_action.assert_awaited_once()
        self.assertEqual(
            self.store.db.execute("SELECT status FROM actions").fetchone()[0],
            "uncertain",
        )

    async def test_five_write_ceiling_survives_reopen(self):
        for index in range(5):
            self.assertTrue(self.store.reserve(str(index), "public", 5, 0, NOW))
            self.store.mark(str(index), "published")
        self.store.close()
        self.store = SocialStore(self.root / "social.sqlite3", NOW + 1)
        self.assertFalse(self.store.reserve("sixth", "public", 5, 0, NOW + 1))
        self.assertTrue(self.store.reserve("next-day", "public", 5, 0, NOW + 86400))

    async def test_spacing_counts_uncertain_attempts(self):
        self.assertTrue(self.store.reserve("one", "public", 5, 7200, NOW))
        self.store.mark("one", "uncertain")
        self.assertFalse(self.store.reserve("two", "public", 5, 7200, NOW + 7199))
        self.assertTrue(self.store.reserve("two", "public", 5, 7200, NOW + 7200))

    async def test_skipped_model_output_never_calls_write_api(self):
        self.context.llm_generate.return_value.completion_text = "__SKIP__"
        candidate = public_candidate(feed(), SELF)
        with patch.object(module.time, "time", return_value=NOW):
            self.assertFalse(
                await self.plugin.submit(self.bot, candidate, "public", 5, 7200)
            )
        self.bot.call_action.assert_not_awaited()

    async def test_own_scan_limits_each_batch_to_two_write_attempts(self):
        value = detail()
        value["comment_num"] = 5
        value["comments"] = [
            dict(value["comments"][0], comment_id=str(i)) for i in range(5)
        ]

        async def action(name, **kwargs):
            if name == "get_qzone_msg_list":
                return {"msglist": [{"tid": value["tid"], "comment_num": 5}]}
            if name == "get_qzone_msg_detail":
                return value
            raise TimeoutError()

        self.bot.call_action.side_effect = action
        with patch.object(module.time, "time", return_value=NOW):
            await self.plugin.reply_on_own_posts(self.bot, {})
        writes = [
            call
            for call in self.bot.call_action.await_args_list
            if call.args[0] == "comment_qzone"
        ]
        self.assertEqual(len(writes), 2)

    async def test_seen_recommendations_do_not_hide_a_new_candidate(self):
        values = [feed(key=str(i)) for i in range(4)]
        for value in values[:3]:
            key = public_candidate(value, SELF)["key"]
            self.store.reserve(key, "public", 5, 0, NOW)
            self.store.mark(key, "skipped")

        async def action(name, **kwargs):
            if name == "get_qzone_public_feeds":
                return {"feeds": values}
            return {"comment": {"id": "actual-receipt"}}

        self.bot.call_action.side_effect = action
        with (
            patch.object(module.time, "time", return_value=NOW),
            patch.object(module, "datetime") as clock,
        ):
            clock.now.return_value.hour = 12
            await self.plugin.comment_recommendation(self.bot, {})
        writes = [
            call
            for call in self.bot.call_action.await_args_list
            if call.args[0] == "comment_qzone_public_feed"
        ]
        self.assertEqual(len(writes), 1)
        self.assertIs(writes[0].kwargs["feed"], values[3])

    async def test_hidden_qq_number_uses_the_actual_public_uid(self):
        value = feed()
        value["cell_user_info"]["user"] = {
            "uin": "",
            "uid": "opaque-public-uid",
            "nick": "空友",
        }
        candidate = public_candidate(value, SELF)
        self.assertIsNotNone(candidate)
        self.assertIs(candidate["feed"], value)

    async def test_one_unavailable_post_does_not_abort_other_reads(self):
        value = detail()
        value["comment_num"] = 1

        async def action(name, **kwargs):
            if name == "get_qzone_msg_list":
                return {
                    "msglist": [
                        {"tid": "unavailable", "comment_num": 1},
                        {"tid": value["tid"], "comment_num": 1},
                    ]
                }
            if name == "get_qzone_msg_detail":
                if kwargs["tid"] == "unavailable":
                    raise RuntimeError("HTTP 500")
                return value
            return {"comment_id": "posted"}

        self.bot.call_action.side_effect = action
        status = {}
        with patch.object(module.time, "time", return_value=NOW):
            await self.plugin.reply_on_own_posts(self.bot, status)
        self.assertEqual(status["own_detail_errors"], 1)
        self.assertEqual(status["own_replies_sent"], 1)


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.WARNING)
    unittest.main()
