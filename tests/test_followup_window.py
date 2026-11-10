"""Verify group-and-sender isolation and fixed expiry of name-based follow-ups."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from astrbot.api.message_components import At, Plain, Reply

spec = importlib.util.spec_from_file_location(
    "jinli_followup_test", root / "plugins/astrbot_plugin_jinli_wake/main.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def event(text="接着说", sender="1001", group="2001", parts=None, **options):
    """Create an event without sending any QQ messages."""
    return SimpleNamespace(
        is_at_or_wake_command=options.get("already_awake", False),
        is_wake=options.get("already_awake", False),
        message_obj=SimpleNamespace(
            raw_message={"post_type": options.get("post_type", "message")}
        ),
        get_platform_id=lambda: options.get("platform", "qq-snowluma"),
        get_self_id=lambda: options.get("account", "10000"),
        get_sender_id=lambda: sender,
        is_private_chat=lambda: group is None,
        get_group_id=lambda: group,
        get_messages=lambda: parts if parts is not None else [Plain(text)],
    )


async def main():
    plugin = module.JinliKeywordWake(
        SimpleNamespace(),
        {
            "enabled": True,
            "platform_id": "qq-snowluma",
            "account_id": "10000",
            "keyword": "龙娘",
            "followup_seconds": 60,
        },
    )
    now = [100.0]
    checks = 0
    with patch.object(module.time, "monotonic", side_effect=lambda: now[0]):

        async def check(message, expected):
            nonlocal checks
            await plugin.wake_for_keyword(message)
            assert message.is_at_or_wake_command is expected
            checks += 1

        await check(event(), False)
        await check(event("龙娘，聊聊"), True)
        now[0] = 159.999
        await check(event(), True)
        await check(event(sender="1002"), False)
        await check(event(group="2002"), False)
        await check(event(group=None), False)
        await check(event(sender="10000"), False)
        await check(event(post_type="notice"), False)
        await check(event(platform="other"), False)
        await check(event(account="10001"), False)
        assert plugin._followup_windows[("2001", "1001")] == 160.0
        now[0] = 160.0
        await check(event(), False)
        assert not plugin._followup_windows
        await check(event("龙娘", sender="1002"), True)
        await check(event(sender="1002"), True)
        await check(event(), False)
        now[0] = 170.0
        await check(event("龙娘", sender="1002"), True)
        assert plugin._followup_windows[("2001", "1002")] == 230.0
        await check(
            event(
                sender="1003",
                parts=[
                    Reply(id=1, message_str="龙娘", sender_id=12),
                    Plain("引用里才有名字"),
                ],
            ),
            False,
        )
        await check(event(sender="1003"), False)
        await check(
            event(
                sender="1004",
                parts=[At(qq="10000"), Plain("你好")],
                already_awake=True,
            ),
            True,
        )
        await check(event(sender="1004"), False)
        plugin.config["enabled"] = False
        await check(event(sender="1002"), False)
        plugin.config["enabled"] = True
        for sender in range(2100):
            await plugin.wake_for_keyword(event("龙娘", sender=str(sender)))
        assert len(plugin._followup_windows) == 2048
        checks += 1
    print(f"Passed {checks} follow-up window checks.")


asyncio.run(main())
