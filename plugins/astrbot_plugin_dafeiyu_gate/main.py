"""大肥鱼·陌生人闸

目的：QQ 那边废弃后，她的长期记忆是 QQ + Telegram 共用的。
如果 Telegram bot 的用户名被陌生人搜到，对方私聊她就能聊到这些记忆。
这个插件只做一件事：**Telegram 私聊里，只让主人通过；陌生人拦在 LLM 之前**。

- 群聊不拦：群里只召回本群来源的记忆，而且有 @/唤醒词门槛。
- 控制台聊天室（webchat）不拦：platforms 默认只有 telegram。
- 拦下后可以回一句固定的拒绝话术，同一个陌生人按冷却时间只回一次。
- 不调用模型、不进历史、不触发记忆召回。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_dafeiyu_gate"

DEFAULTS = {
    "enabled": True,
    "platforms": "telegram",
    "owner_ids": "10000000",
    "block_private_strangers": True,
    "stranger_reply": "这个号只跟主人聊天，不加陌生人。",
    "reply_cooldown_hours": 24.0,
}


def _split_ids(value) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = re.split(r"[,;\s]+", str(value or ""))
    return {str(x).strip() for x in items if str(x).strip()}


class DafeiyuGate(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        cfg = dict(DEFAULTS)
        if isinstance(config, dict):
            for k in DEFAULTS:
                if config.get(k) is not None:
                    cfg[k] = config[k]
        self.cfg = cfg
        self.owner_ids = _split_ids(cfg.get("owner_ids"))
        self.platforms = {
            p.casefold() for p in _split_ids(cfg.get("platforms"))
        }
        self.reply_text = str(cfg.get("stranger_reply") or "").strip()
        self.cooldown = max(0.0, float(cfg.get("reply_cooldown_hours") or 0)) * 3600
        self._dir = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
        self._state_path = os.path.join(self._dir, "state.json")
        self._state: dict[str, float] = self._load()
        logger.info(
            f"[dafeiyu-gate] 已加载：平台={sorted(self.platforms)}，"
            f"主人={sorted(self.owner_ids) or '（空）'}"
        )

    # ---------- 状态 ----------

    def _load(self) -> dict[str, float]:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                return {str(k): float(v) for k, v in raw.items() if v}
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu-gate] 读取状态失败: {e}")
        return {}

    def _save(self) -> None:
        try:
            os.makedirs(self._dir, exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._state_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu-gate] 保存状态失败: {e}")

    def _cooldown_ok(self, sender: str) -> bool:
        if self.cooldown <= 0:
            return True
        last = float(self._state.get(sender) or 0)
        return (time.time() - last) >= self.cooldown

    def _mark_replied(self, sender: str) -> None:
        now = time.time()
        # 顺手清掉 30 天前的记录，别让文件一直长
        cutoff = now - 30 * 86400
        self._state = {k: v for k, v in self._state.items() if v >= cutoff}
        self._state[sender] = now
        self._save()

    # ---------- 拦截 ----------

    @filter.event_message_type(
        filter.EventMessageType.ALL, priority=sys.maxsize - 100
    )
    async def gate_private(self, event: AstrMessageEvent):
        try:
            if not self.cfg.get("enabled"):
                return
            if not self.cfg.get("block_private_strangers"):
                return
            platform = (event.get_platform_name() or "").strip().casefold()
            if platform not in self.platforms:
                return
            if not event.is_private_chat():
                return
            sender = str(event.get_sender_id() or "").strip()
            if not sender or sender in self.owner_ids:
                return

            # 先掐断传播：后面的 LLM、记忆召回、插件钩子都不会跑
            event.stop_event()
            logger.info(f"[dafeiyu-gate] 拦下陌生人私聊：{platform}/{sender}")

            if not self.reply_text or not self._cooldown_ok(sender):
                return
            try:
                await event.send(MessageChain([Plain(self.reply_text)]))
                self._mark_replied(sender)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[dafeiyu-gate] 回绝消息发送失败: {e}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dafeiyu-gate] 处理失败: {e}")
