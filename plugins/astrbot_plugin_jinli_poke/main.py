import time
from collections import OrderedDict

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star


class JinliPoke(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_poke = OrderedDict()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def respond_to_poke(self, event: AstrMessageEvent):
        """Route a poke aimed at Jinli through the existing persona chat pipeline.

        Args:
            event: An incoming OneBot message or notification.
        """
        if not self.config.get("enabled", False):
            return
        if event.get_platform_id() != self.config.get("platform_id"):
            return
        self_id = str(event.message_obj.self_id)
        if self_id != str(self.config.get("account_id")):
            return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "notice" or raw.get("sub_type") != "poke":
            return

        sender_id = str(raw.get("user_id") or "")
        if (
            str(raw.get("target_id")) != self_id
            or not sender_id
            or sender_id == self_id
        ):
            event.stop_event()
            return

        # Reserve the conversation cooldown before yielding to the model pipeline.
        session = event.unified_msg_origin
        now = time.monotonic()
        previous = self._last_poke.get(session)
        if previous is not None and now - previous < 15:
            event.stop_event()
            return
        self._last_poke[session] = now
        self._last_poke.move_to_end(session)
        if len(self._last_poke) > 1024:
            self._last_poke.popitem(last=False)

        prompt = (
            "[QQ 互动通知：当前这位用户刚刚戳了戳你，并没有发送文字。]"
            "请以烬璃的口吻自然回应这次戳一戳，简短一两句话，可以俏皮一点。"
            "不要编造对方说过的话，不要解释系统或配置，也不要把这条通知当作对方原话。"
        )
        event.message_obj.message = [Plain(prompt)]
        event.message_obj.message_str = prompt
        event.message_str = prompt
        event.is_at_or_wake_command = True
        event.is_wake = True
        self.logger.info("Jinli accepted an incoming poke for a persona reply.")

    async def initialize(self):
        """Report whether the account-scoped poke listener is enabled."""
        self.logger.info(
            "Jinli poke replies enabled: %s.", self.config.get("enabled", False)
        )
