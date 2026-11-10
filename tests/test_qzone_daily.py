import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PLUGIN = ROOT / "plugins/astrbot_plugin_jinli_qzone"
spec = importlib.util.spec_from_file_location(
    "jinli_qzone_test", PLUGIN / "main.py", submodule_search_locations=[str(PLUGIN)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
qzone = sys.modules["jinli_qzone_test.qzone"]


class QzoneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = SimpleNamespace(
            get_login_info=AsyncMock(return_value={"user_id": 10000}),
            get_cookies=AsyncMock(
                return_value={"cookies": "uin=o10000; skey=fake; p_skey=fake"}
            ),
        )
        self.context = SimpleNamespace(
            get_platform_inst=lambda _: SimpleNamespace(bot=self.bot)
        )
        with patch.object(
            module.StarTools, "get_data_dir", return_value=Path(self.temp.name)
        ):
            self.plugin = module.JinliQzone(
                self.context,
                {
                    "enabled": True,
                    "daily_time": "19:15",
                    "platform_id": "jinli-personal",
                    "account_id": "10000",
                },
            )

    async def test_read_only_check_and_publish_parameters(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "tid": "test-tid"})

        original = httpx.AsyncClient

        def client(**kwargs):
            return original(transport=httpx.MockTransport(handle), **kwargs)

        with patch.object(qzone.httpx, "AsyncClient", side_effect=client):
            await qzone.request_qzone(self.bot, "10000")
            result = await qzone.request_qzone(self.bot, "10000", "测试动态")
        self.assertEqual([r.method for r in requests], ["GET", "POST"])
        self.assertIn(b"who_can_see=1", requests[1].content)
        self.assertEqual(result["tid"], "test-tid")

    async def test_wrong_account_and_missing_login_stop_before_network(self):
        with patch.object(qzone.httpx, "AsyncClient") as network:
            with self.assertRaises(qzone.QzoneError):
                await qzone.request_qzone(self.bot, "123")
            self.bot.get_cookies.return_value = {"cookies": "uin=o10000"}
            with self.assertRaises(qzone.QzoneError):
                await qzone.request_qzone(self.bot, "10000")
            network.assert_not_called()

    async def test_jsonp_and_rejection_are_sanitized(self):
        self.assertEqual(qzone.parse_response('_Callback({"code":0});')["code"], 0)
        self.assertEqual(
            qzone.parse_response("/* callback */ cb({code:0,});")["code"], 0
        )
        self.assertEqual(
            qzone.parse_response("cb({code:0,data:undefined,text:'undefined'});")[
                "text"
            ],
            "undefined",
        )
        with self.assertRaisesRegex(qzone.QzoneError, "^QZONE_REJECTED_-3000$"):
            qzone.parse_response('{"code":-3000,"message":"secret-cookie"}')

    async def test_daily_post_is_not_repeated_after_restart(self):
        self.plugin.generate_post = AsyncMock(
            return_value="今天把尾巴盘成了一个圈，正适合守着一点暖意发呆。"
        )
        with patch.object(
            module, "request_qzone", new=AsyncMock(return_value={"tid": "test-tid"})
        ) as send:
            await self.plugin.publish_daily()
            await self.plugin.publish_daily()
            self.assertEqual(send.await_count, 2)
            self.plugin.state = json.loads(
                self.plugin.state_path.read_text(encoding="utf-8")
            )
            await self.plugin.publish_daily()
            self.assertEqual(send.await_count, 2)
        self.assertEqual(self.plugin.state["result"], "published")

    async def test_uncertain_publish_failure_is_not_retried(self):
        self.plugin.generate_post = AsyncMock(
            return_value="本龙今天也想安静地守着一点暖意。"
        )
        with patch.object(
            module,
            "request_qzone",
            new=AsyncMock(
                side_effect=[{}, qzone.QzoneError("QZONE_NETWORK_OR_HTTP_ERROR")]
            ),
        ) as send:
            await self.plugin.publish_daily()
            await self.plugin.publish_daily()
            self.assertEqual(send.await_count, 2)
        self.assertEqual(self.plugin.state["result"], "QZONE_NETWORK_OR_HTTP_ERROR")

    async def test_schedule_and_disabled_switch(self):
        self.plugin.preflight = AsyncMock()
        await self.plugin.initialize()
        job = self.plugin.scheduler.get_job("jinli_daily_qzone")
        self.assertEqual(str(job.trigger.timezone), "Asia/Shanghai")
        self.assertEqual(job.next_run_time.strftime("%H:%M"), "19:15")
        self.assertGreater(job.next_run_time, datetime.now(module.TZ))
        await self.plugin.terminate()
        self.plugin.config["enabled"] = False
        with patch.object(module, "request_qzone", new=AsyncMock()) as send:
            await self.plugin.publish_daily()
            send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
