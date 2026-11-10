import asyncio
from collections import deque

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class JinliFriends(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._lock = asyncio.Lock()
        self._accepted = deque(maxlen=512)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def accept_friend_request(self, event: AstrMessageEvent):
        """Approve incoming friend requests for the configured account.

        Args:
            event: The incoming OneBot event wrapped by AstrBot.
        """
        if not self.config.get("enabled", False):
            return
        if event.get_platform_id() != self.config.get("platform_id"):
            return
        if str(event.message_obj.self_id) != str(self.config.get("account_id")):
            return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "request" or raw.get("request_type") != "friend":
            return

        # Request events must not continue into the normal chat pipeline.
        event.stop_event()
        flag = str(raw.get("flag") or "")
        if not flag:
            self.logger.warning("Friend request has no flag; leaving it pending.")
            return
        async with self._lock:
            if flag in self._accepted:
                return
            try:
                await event.bot.call_action(
                    "set_friend_add_request", flag=flag, approve=True
                )
            except Exception:
                self.logger.error("Friend request approval failed; leaving it pending.")
                return
            self._accepted.append(flag)
            self.logger.info(
                "Approved an incoming friend request on the Jinli account."
            )

    async def initialize(self):
        """Log the current feature state after the plugin loads."""
        self.logger.info(
            "Jinli incoming friend approval enabled: %s.",
            self.config.get("enabled", False),
        )
