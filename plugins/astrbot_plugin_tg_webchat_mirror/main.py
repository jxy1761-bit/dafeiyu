"""大肥鱼 · TG 同步到 WebChat

把 Telegram 的聊天内容镜像到 WebChat 的共享会话：
1) 写进 webchat 的平台消息历史，App 重开、换设备都能看到；
2) 通过本机 Hub 的 /hub/ingest 实时广播给正在看的设备。

只镜像 Telegram → WebChat，不反向，避免两端口互相回环。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain, Record, Video
from astrbot.api.star import Context, Star
from astrbot.core import db_helper

DEFAULTS = {
    "enabled": True,
    "target_session": "shared",
    "mirror_user": True,
    "mirror_bot": True,
    "sender_prefix": True,
    "hub_url": "http://127.0.0.1:6186/hub/ingest",
}


def _component_text(component) -> str:
    if isinstance(component, Plain):
        return component.text or ""
    if isinstance(component, Image):
        return "[图片]"
    if isinstance(component, Record):
        return "[语音]"
    if isinstance(component, Video):
        return "[视频]"
    if isinstance(component, File):
        return "[文件]"
    return ""


def _chain_text(chain) -> str:
    parts = [_component_text(component) for component in chain or []]
    return "".join(part for part in parts if part).strip()


class TgWebchatMirror(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        cfg = dict(DEFAULTS)
        if isinstance(config, dict):
            for key in DEFAULTS:
                if config.get(key) is not None:
                    cfg[key] = config[key]
        self.cfg = cfg
        logger.info(
            "[tg-webchat-mirror] 已加载（session=%s，用户消息=%s，她的回复=%s）",
            cfg["target_session"],
            cfg["mirror_user"],
            cfg["mirror_bot"],
        )

    def _is_telegram(self, event: AstrMessageEvent) -> bool:
        try:
            return event.get_platform_name() == "telegram"
        except Exception:
            return False

    async def initialize(self):
        # 主动消息（proactive_tg）不走回复管线，只会调平台的 send_by_session；
        # 包一层把这类消息也同步过去，正常回复仍走 after_message_sent，不会重复。
        try:
            platform = self.context.get_platform_inst("telegram") or self.context.get_platform(
                "telegram"
            )
            if platform is None or getattr(platform, "_tg_mirror_wrapped", False):
                return
            original = platform.send_by_session

            async def wrapped(session, message_chain):
                await original(session, message_chain)
                text = _chain_text(getattr(message_chain, "chain", None))
                if text:
                    await self._mirror("bot", "bot", "bot", text)

            platform.send_by_session = wrapped
            platform._tg_mirror_wrapped = True
            logger.info("[tg-webchat-mirror] 已接管 Telegram 主动发送")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tg-webchat-mirror] 接管主动发送失败：%s", exc)

    async def _mirror(self, role: str, sender_id: str, sender_name: str, text: str) -> None:
        session_id = str(self.cfg["target_session"] or "shared")
        await db_helper.insert_platform_message_history(
            platform_id="webchat",
            user_id=session_id,
            content={"type": role, "message": [{"type": "plain", "text": text}]},
            sender_id=sender_id or None,
            sender_name=sender_name or None,
        )
        try:
            import aiohttp

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as session:
                await session.post(
                    str(self.cfg["hub_url"]),
                    json={
                        "session_id": session_id,
                        "role": role,
                        "sender": sender_name,
                        "text": text,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            # Hub 没开（或只开了 AstrBot）时历史已经写好，下次刷新能看到。
            logger.debug("[tg-webchat-mirror] 实时推送失败：%s", exc)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def mirror_incoming(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True) or not self.cfg.get("mirror_user", True):
            return
        if not self._is_telegram(event):
            return
        try:
            text = _chain_text(getattr(event.message_obj, "message", None))
            if not text:
                text = (event.get_message_str() or "").strip()
            if not text:
                return
            sender = (event.get_sender_name() or event.get_sender_id() or "TG").strip()
            if self.cfg.get("sender_prefix", True):
                text = f"[TG·{sender}] {text}"
            await self._mirror("user", event.get_sender_id(), sender, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tg-webchat-mirror] 同步来消息失败：%s", exc)

    @filter.after_message_sent()
    async def mirror_reply(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True) or not self.cfg.get("mirror_bot", True):
            return
        if not self._is_telegram(event):
            return
        try:
            result = event.get_result()
            text = _chain_text(getattr(result, "chain", None))
            if not text:
                return
            await self._mirror("bot", "bot", "bot", text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tg-webchat-mirror] 同步她的回复失败：%s", exc)
