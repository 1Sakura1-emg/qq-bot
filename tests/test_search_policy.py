import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_main_agent import _apply_web_search_tools
from astrbot.core.tools.web_search_tools import AnySearchWebSearchTool

spec = importlib.util.spec_from_file_location(
    "jinli_search_test", ROOT / "plugins/astrbot_plugin_jinli_search/main.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SearchPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = {
            "web_search": True,
            "websearch_provider": "anysearch",
            "websearch_anysearch_key": [],
        }
        self.context = SimpleNamespace(
            get_config=lambda **kwargs: {"provider_settings": self.settings},
            get_llm_tool_manager=lambda: SimpleNamespace(
                get_builtin_tool=lambda cls: cls()
            ),
        )
        self.plugin = module.JinliSearch(
            self.context,
            {
                "enabled": True,
                "platform_id": "jinli-personal",
                "account_id": "10000",
            },
        )
        self.event = SimpleNamespace(
            get_platform_id=lambda: "jinli-personal",
            message_obj=SimpleNamespace(self_id="10000"),
            unified_msg_origin="jinli-personal:GroupMessage:10002",
            message_str="她是谁？",
        )

    async def test_builtin_search_is_available_without_a_new_key(self):
        request = ProviderRequest(prompt="她是谁？")
        await _apply_web_search_tools(self.event, request, self.context)
        await self.plugin.prepare_search(self.event, request)
        self.assertEqual(request.func_tool.names(), ["web_search_anysearch"])
        self.assertIn("先提炼明确关键词搜索", request.system_prompt)
        self.assertEqual(request.prompt, "她是谁？")

    async def test_only_read_only_search_tool_survives(self):
        request = ProviderRequest(func_tool=ToolSet())
        request.func_tool.add_tool(AnySearchWebSearchTool())
        for name in ["send_message_to_user", "future_task", "astrbot_execute_shell"]:
            request.func_tool.add_tool(
                FunctionTool(
                    name=name,
                    description="test-only",
                    parameters={"type": "object", "properties": {}},
                )
            )
        await self.plugin.prepare_search(self.event, request)
        self.assertEqual(request.func_tool.names(), ["web_search_anysearch"])

    async def test_other_accounts_and_disabled_policy_are_untouched(self):
        request = ProviderRequest(system_prompt="original")
        self.plugin.config["enabled"] = False
        await self.plugin.prepare_search(self.event, request)
        self.plugin.config["enabled"] = True
        self.event.message_obj.self_id = "99999"
        await self.plugin.prepare_search(self.event, request)
        self.assertEqual(request.system_prompt, "original")

    async def test_disabled_search_does_not_claim_search_capability(self):
        request = ProviderRequest(system_prompt="original", func_tool=ToolSet())
        self.settings["web_search"] = False
        await self.plugin.prepare_search(self.event, request)
        self.assertEqual(request.system_prompt, "original")

    async def test_unavailable_search_tool_does_not_claim_capability(self):
        request = ProviderRequest(system_prompt="original", func_tool=ToolSet())
        await self.plugin.prepare_search(self.event, request)
        self.assertEqual(request.system_prompt, "original")

    async def test_real_chat_runner_keeps_the_configured_search_tool(self):
        model = next(
            x
            for x in json.loads(
                (ROOT / "tests/fixtures/model.json").read_text(encoding="utf-8-sig")
            )["provider"]
            if x["id"] == "deepseek-dragon/deepseek-flash"
        )
        runner = ToolLoopAgentRunner()
        runner.provider = SimpleNamespace(provider_config=model)
        runner.req = ProviderRequest(func_tool=ToolSet())
        runner.req.func_tool.add_tool(AnySearchWebSearchTool())
        self.assertIn("tool_use", model["modalities"])
        self.assertIs(runner._func_tool_for_provider(), runner.req.func_tool)
        self.assertIn("image", model["modalities"])

    async def test_original_missing_capability_reproduces_tool_removal(self):
        runner = ToolLoopAgentRunner()
        runner.provider = SimpleNamespace(
            provider_config={"modalities": ["text", "image"]}
        )
        runner.req = ProviderRequest(func_tool=ToolSet())
        runner.req.func_tool.add_tool(AnySearchWebSearchTool())
        self.assertIsNone(runner._func_tool_for_provider())

    async def test_established_lyric_fragment_is_looked_up_before_model_answer(self):
        self.event.message_str = "我们之间的回忆"
        request = ProviderRequest(
            contexts=[
                {"role": "user", "content": "来接歌词吧"},
                {"role": "user", "content": "我们之间的回忆"},
                {"role": "assistant", "content": "这句不熟"},
            ],
            func_tool=ToolSet(),
        )
        request.func_tool.add_tool(AnySearchWebSearchTool())
        result = SimpleNamespace(
            title="示例歌曲", url="https://example.com/song", snippet="歌曲资料"
        )
        with patch.object(
            module, "_anysearch_search", new=AsyncMock(return_value=[result])
        ) as search:
            await self.plugin.prepare_search(self.event, request)
            await self.plugin.prepare_search(
                self.event,
                ProviderRequest(contexts=request.contexts, func_tool=request.func_tool),
            )
        search.assert_awaited_once()
        self.assertIn("已经检索完成", request.system_prompt)
        self.assertTrue(request.extra_user_content_parts[-1]._no_save)

    async def test_unrelated_private_text_is_not_used_as_a_lyric_query(self):
        self.event.message_str = "我的工作安排变了"
        request = ProviderRequest(
            contexts=[
                {"role": "user", "content": "来接歌词吧"},
                {"role": "user", "content": "这是之前的歌曲片段"},
            ],
            func_tool=ToolSet(),
        )
        request.func_tool.add_tool(AnySearchWebSearchTool())
        with patch.object(module, "_anysearch_search", new=AsyncMock()) as search:
            await self.plugin.prepare_search(self.event, request)
        search.assert_not_awaited()

    async def test_generated_baike_id_is_replaced_by_an_actual_source(self):
        extras = {"jinli_verified_search_urls": ["https://example.com/jay-chou/song"]}
        self.event.get_extra = lambda key, default=None: extras.get(key, default)
        response = SimpleNamespace(
            completion_text="周杰伦《一路向北》 https://baike.baidu.com/item/song/22760585"
        )
        await self.plugin.validate_source_links(self.event, response)
        self.assertNotIn("22760585", response.completion_text)
        self.assertIn("https://example.com/jay-chou/song", response.completion_text)

    async def test_actual_source_url_is_preserved(self):
        url = "https://example.com/jay-chou/song"
        self.event.get_extra = lambda key, default=None: [url]
        response = SimpleNamespace(completion_text=f"[周杰伦版本]({url})")
        await self.plugin.validate_source_links(self.event, response)
        self.assertEqual(response.completion_text, f"[周杰伦版本]({url})")

    async def test_second_search_results_extend_instead_of_replacing_sources(self):
        extras = {"jinli_verified_search_urls": ["https://example.com/first"]}
        self.event.get_extra = lambda key, default=None: extras.get(key, default)
        self.event.set_extra = lambda key, value: extras.__setitem__(key, value)
        result = SimpleNamespace(
            content=[
                SimpleNamespace(
                    text=json.dumps(
                        {"results": [{"url": "https://example.com/jay-chou/song"}]}
                    )
                )
            ]
        )
        await self.plugin.remember_search_urls(
            self.event, SimpleNamespace(name="web_search_anysearch"), {}, result
        )
        self.assertEqual(len(extras["jinli_verified_search_urls"]), 2)

    async def test_no_search_does_not_change_an_unrelated_response(self):
        self.event.get_extra = lambda key, default=None: default
        response = SimpleNamespace(completion_text="https://example.com/user-supplied")
        await self.plugin.validate_source_links(self.event, response)
        self.assertEqual(response.completion_text, "https://example.com/user-supplied")


if __name__ == "__main__":
    unittest.main()
