import asyncio
import importlib.util
import sys
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from astrbot.api.message_components import Plain, Reply

spec = importlib.util.spec_from_file_location(
    "jinli_wake_test", root / "plugins/astrbot_plugin_jinli_wake/main.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
plugin = object.__new__(module.JinliKeywordWake)
plugin.config = {
    "enabled": True,
    "platform_id": "qq-snowluma",
    "account_id": "10000",
    "keyword": "龙娘",
    "followup_seconds": 0,
}
plugin._followup_windows = OrderedDict()


async def main():
    cases = [
        ("prefix", [Plain("龙娘，你在吗")], {}, True),
        ("middle", [Plain("大家好，龙娘能介绍一下自己吗")], {}, True),
        ("split_text", [Plain("龙"), Plain("娘，看看这个")], {}, True),
        ("unrelated", [Plain("大家今天吃什么")], {}, False),
        (
            "quoted_only",
            [Reply(id=1, message_str="龙娘", sender_id=123), Plain("这句话什么意思")],
            {},
            False,
        ),
        ("private", [Plain("龙娘")], {"private": True}, False),
        ("self", [Plain("龙娘")], {"sender": "10000"}, False),
        ("other_adapter", [Plain("龙娘")], {"platform": "other"}, False),
        ("other_account", [Plain("龙娘")], {"self_id": "10001"}, False),
        ("notice", [Plain("龙娘")], {"post_type": "notice"}, False),
    ]
    for name, parts, opts, expected in cases:
        event = SimpleNamespace(
            is_at_or_wake_command=False,
            is_wake=False,
            message_obj=SimpleNamespace(
                raw_message={"post_type": opts.get("post_type", "message")}
            ),
            get_platform_id=lambda opts=opts: opts.get("platform", "qq-snowluma"),
            get_self_id=lambda opts=opts: opts.get("self_id", "10000"),
            get_sender_id=lambda opts=opts: opts.get("sender", "123"),
            is_private_chat=lambda opts=opts: opts.get("private", False),
            get_group_id=lambda: "10002",
            get_messages=lambda parts=parts: parts,
        )
        await plugin.wake_for_keyword(event)
        assert event.is_at_or_wake_command is expected, name
        assert event.get_messages() == parts, name
    event.is_at_or_wake_command = True
    await plugin.wake_for_keyword(event)
    assert event.is_at_or_wake_command, "existing wake must be preserved"
    print(f"Passed {len(cases) + 1} keyword wake checks.")


asyncio.run(main())
