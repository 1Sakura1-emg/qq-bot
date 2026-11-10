import time
from collections import OrderedDict

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star


class JinliKeywordWake(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._followup_windows = OrderedDict()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=90)
    async def wake_for_keyword(self, event: AstrMessageEvent):
        """Wake on Jinli's name or a sender's unexpired group follow-up window.

        Args:
            event: An incoming OneBot event, whose own text is inspected.
        """
        if not self.config.get("enabled", False):
            return
        if event.get_platform_id() != self.config.get("platform_id"):
            return
        if str(event.get_self_id()) != str(self.config.get("account_id")):
            return
        if event.is_private_chat() or not event.get_group_id():
            return
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict) or raw.get("post_type") != "message":
            return
        text = "".join(
            part.text for part in event.get_messages() if isinstance(part, Plain)
        )
        keyword = str(self.config.get("keyword", "龙娘")).strip()
        now = time.monotonic()
        while self._followup_windows:
            oldest = next(iter(self._followup_windows))
            if self._followup_windows[oldest] > now:
                break
            self._followup_windows.popitem(last=False)
        window_key = (str(event.get_group_id()), str(event.get_sender_id()))
        seconds = max(0, int(self.config.get("followup_seconds", 60)))
        if keyword and keyword in text:
            if seconds:
                self._followup_windows[window_key] = now + seconds
                self._followup_windows.move_to_end(window_key)
                if len(self._followup_windows) > 2048:
                    self._followup_windows.popitem(last=False)
            event.is_at_or_wake_command = True
            event.is_wake = True
        elif seconds and self._followup_windows.get(window_key, 0) > now:
            # Only a fresh name mention extends the fixed-duration window.
            event.is_at_or_wake_command = True
            event.is_wake = True

    async def initialize(self):
        """Report whether group keyword wake-up is enabled."""
        self.logger.info(
            "Jinli group name wake enabled: %s; per-group, per-sender follow-up: %s seconds.",
            self.config.get("enabled", False),
            self.config.get("followup_seconds", 60),
        )
