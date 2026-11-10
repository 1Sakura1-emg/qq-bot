import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, StarTools

from .qzone import QzoneError, request_qzone

TZ = ZoneInfo("Asia/Shanghai")


class JinliQzone(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_jinli_qzone")
        self.state_path = self.data_dir / "state.json"
        self.status_path = self.data_dir / "status.json"
        self.state = {}
        self.scheduler = AsyncIOScheduler(timezone=TZ)
        self.preflight_task = None
        self.lock = asyncio.Lock()

    async def initialize(self):
        """Load the duplicate guard and register the daily publication job."""
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(self.state, dict):
                raise ValueError("Invalid state")
        except FileNotFoundError:
            self.state = {}
        except (OSError, ValueError):
            self.logger.error("Qzone state is invalid; automatic publishing stopped.")
            return
        if self.config.get("enabled", False):
            hour, minute = map(int, self.config["daily_time"].split(":"))
            self.scheduler.add_job(
                self.publish_daily,
                CronTrigger(hour=hour, minute=minute, timezone=TZ),
                id="jinli_daily_qzone",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )
            self.scheduler.start()
            self.logger.info(
                "Jinli daily Qzone publication enabled at %02d:%02d Asia/Shanghai.",
                hour,
                minute,
            )
        self.preflight_task = asyncio.create_task(self.preflight())

    async def generate_post(self) -> str:
        """Generate one persona-based post without reading private chat history.

        Returns:
            A validated, short, plain-text post.

        Raises:
            ValueError: If the generated content is empty, too long, or repeated.
        """
        persona = await self.context.persona_manager.get_persona("jinli")
        history = self.state.get("history", [])[-7:]
        response = await self.context.llm_generate(
            chat_provider_id=self.config["provider_id"],
            system_prompt=persona.system_prompt + "\n" + self.config["post_prompt"],
            prompt=(
                f"北京时间：{datetime.now(TZ):%Y年%m月%d日 %H:%M}。"
                "请写今天要发在自己 QQ 空间的一条日常碎碎念，只输出正文。"
                "这是角色创作，不要声称真的外出、看过视频或做过未执行的事情。"
                "不要提及任何真实聊天对象、私聊内容、QQ号、密码或密钥。"
                "可以写角色世界里的小事、轻松感想或对天气季节的想象，"
                "不要声称知道现实当地天气。不要广告、链接、@、话题标签。"
                "最近发过的内容如下，只用于避免重复："
                + json.dumps(history, ensure_ascii=False)
            ),
            max_tokens=384,
            temperature=0.8,
        )
        content = (response.completion_text or "").strip().strip('"“”')
        if not 5 <= len(content) <= 240 or content in history:
            raise ValueError("Invalid or repeated post")
        if any(marker in content.lower() for marker in ("http://", "https://", "sk-")):
            raise ValueError("Unexpected link or credential-like text")
        return content

    async def preflight(self):
        """Check account access and save a preview without publishing."""
        status = {"checked_at": datetime.now(TZ).isoformat(), "published": False}
        for _ in range(20):
            await asyncio.sleep(3)
            platform = self.context.get_platform_inst(self.config["platform_id"])
            if platform is not None:
                try:
                    await request_qzone(platform.bot, self.config["account_id"])
                    status["connection"] = "ready"
                    break
                except QzoneError as exc:
                    status["connection"] = str(exc)
                    if str(exc) != "QQ_CONNECTION_UNAVAILABLE":
                        break
        else:
            status["connection"] = "QQ_CONNECTION_UNAVAILABLE"
        try:
            status["preview"] = await self.generate_post()
        except Exception:
            status["preview_error"] = "POST_GENERATION_FAILED"
        self.status_path.write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.logger.info("Jinli Qzone preflight result: %s.", status["connection"])

    async def publish_daily(self):
        """Generate and publish at most one post per day, without blind retries."""
        if not self.config.get("enabled", False):
            return
        async with self.lock:
            today = datetime.now(TZ).date().isoformat()
            if self.state.get("attempt_date") == today:
                return
            try:
                platform = self.context.get_platform_inst(self.config["platform_id"])
                if platform is None:
                    raise QzoneError("QQ_CONNECTION_UNAVAILABLE")
                # Check login before generating text or attempting a publication.
                await request_qzone(platform.bot, self.config["account_id"])
                content = await self.generate_post()
                self.state.update(attempt_date=today, result="attempting")
                self.state_path.write_text(
                    json.dumps(self.state, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                # A timeout can happen after a successful post. Never retry it blindly.
                result = await request_qzone(
                    platform.bot, self.config["account_id"], content
                )
                self.state.update(
                    result="published",
                    published_at=datetime.now(TZ).isoformat(),
                    post_id=str(result.get("tid") or result.get("t1_tid") or ""),
                    history=(self.state.get("history", []) + [content])[-7:],
                )
                self.logger.info("Jinli published the daily Qzone text post.")
            except QzoneError as exc:
                self.state["result"] = str(exc)
                self.logger.error("Jinli Qzone publication failed: %s.", exc)
            except Exception:
                self.state["result"] = "POST_GENERATION_OR_STORAGE_FAILED"
                self.logger.error("Jinli Qzone generation or storage failed.")
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    async def terminate(self):
        """Stop jobs and cancel a pending read-only preflight on unload."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if self.preflight_task and not self.preflight_task.done():
            self.preflight_task.cancel()
            try:
                await self.preflight_task
            except asyncio.CancelledError:
                pass
