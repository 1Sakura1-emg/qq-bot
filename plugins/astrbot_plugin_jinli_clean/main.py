import re

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star


class JinliClean(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config

    @filter.on_decorating_result()
    async def collapse_blank_lines(self, event: AstrMessageEvent):
        """发送前把回复里的连续换行压成单个，避免 QQ 显示大间距。

        Args:
            event: 携带回复结果的消息事件。
        """
        if not self.config.get("enabled", True):
            return
        result = event.get_result()
        if result is None:
            return
        for comp in result.chain:
            if isinstance(comp, Plain):
                comp.text = re.sub(r"[\r\n]+", "\n", comp.text)
