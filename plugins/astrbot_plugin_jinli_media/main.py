import asyncio

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star


class JinliMedia(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.event_message_type(filter.EventMessageType.ALL, priority=90)
    async def recognize_voice(self, event: AstrMessageEvent):
        """Convert QQ voice messages to text before the persona sees them.

        Args:
            event: An incoming message from the configured QQ account.
        """
        if not self.config.get("enabled", False):
            return
        if event.get_platform_id() != self.config.get("platform_id"):
            return
        if str(event.message_obj.self_id) != str(self.config.get("account_id")):
            return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict) or raw.get("post_type") != "message":
            return

        groups = [(event.get_messages(), event.message_obj.message_id, "语音内容")]
        for component in event.get_messages():
            if isinstance(component, Reply) and component.chain:
                groups.append((component.chain, component.id, "引用的语音内容"))

        additions = []
        for chain, message_id, label in groups:
            if not any(isinstance(component, Record) for component in chain):
                continue
            text = ""
            for attempt in range(2):
                try:
                    result = await asyncio.wait_for(
                        event.bot.call_action(
                            "fetch_ptt_text", message_id=str(message_id)
                        ),
                        timeout=15,
                    )
                    text = str(result.get("text") or "").strip()
                    if text:
                        break
                except Exception:
                    pass
                if attempt < 1:
                    await asyncio.sleep(0.8)
            if text:
                replacement = f"[{label}转写成功] {text}"
                event.set_extra("jinli_voice_transcribed", True)
                self.logger.info("Converted a QQ voice message to text.")
            else:
                replacement = (
                    f"[{label}识别失败：请告知用户暂时没听清，让用户重发或打字，"
                    "不要猜测语音内容。]"
                )
                self.logger.warning("QQ voice recognition was unavailable.")
            chain[:] = [c for c in chain if not isinstance(c, Record)]
            chain.append(Plain(replacement))
            additions.append(replacement)

        # Some adapters send a sticker summary without an accompanying image.
        for segment in raw.get("message", []):
            if segment.get("type") == "mface":
                summary = str(segment.get("data", {}).get("summary") or "").strip()
                if summary:
                    summary = "[表情包描述] " + summary[:160]
                    event.message_obj.message.append(Plain(summary))
                    additions.append(summary)
        if additions:
            suffix = "\n".join(additions)
            event.message_str = (event.message_str + "\n" + suffix).strip()
            event.message_obj.message_str = event.message_str

    @filter.on_llm_request()
    async def explain_media_input(
        self, event: AstrMessageEvent, request: ProviderRequest
    ):
        """Describe the real media capabilities and any successful transcript.

        Args:
            event: The message associated with the model request.
            request: The request that will be sent to the chat model.
        """
        if not self.config.get("enabled", False):
            return
        if event.get_platform_id() != self.config.get("platform_id"):
            return
        if str(event.message_obj.self_id) != str(self.config.get("account_id")):
            return
        request.system_prompt += (
            "\n<media_capabilities>当前聊天已开启图片理解和 QQ 语音转文字。"
            "本次实际附带的图片可以看图理解；标有‘语音内容转写成功’或"
            "‘引用的语音内容转写成功’的内容是已经识别出的原话，不是占位符。"
            "依据实际图片和转写回答，不要沿用旧对话中‘功能没开’的说法。"
            "没有实际附件或转写时，不要猜测内容；不能从转写推断音色。"
            "</media_capabilities>"
        )
        if event.get_extra("jinli_voice_transcribed"):
            request.system_prompt += (
                "\n<voice_status>这条消息的语音已转写成功，原话就在当前用户输入中。"
                "请直接回答该原话；若对方问能否听到或说了什么，可以明确说已识别，"
                "并复述原话。不要声称未收到语音内容或尚未开启转写。</voice_status>"
            )

    async def initialize(self):
        """Report the state of the account-scoped QQ voice bridge."""
        self.logger.info(
            "Jinli QQ voice-to-text enabled: %s.", self.config.get("enabled", False)
        )
