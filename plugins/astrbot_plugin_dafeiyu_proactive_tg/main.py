"""大肥鱼 · 想你了（Telegram 主动找人）

和「汇报/抽查/任务提醒」相反：这个插件只在 Telegram 私聊里，由她自己
判断此刻想不想找主人，想就发一条没有任务味的话。带静默、每日上限、
被无视退避和失败冷却；`/戳我` 可手动测一次。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .material import (
    build_material_block,
    load_relationship_state,
    material_by_label,
    select_materials,
    sensitive_memory_ids,
    text_mentions_material,
)
from .gate import gate_candidate, is_distressed
from .llm_gate import decide as decide_llm_gate, judge as judge_llm_gate

DEFAULTS = {
    "enabled": True,
    "owner_telegram": "10000000",
    "owner_umo": "webchat:FriendMessage:webchat!astrbot!shared",
    "check_minutes": 20,
    "min_silence_minutes": 45,
    "quiet_start": "23:30",
    "quiet_end": "08:30",
    "min_gap_minutes": 180,
    "max_per_day": 2,
    "consider_probability": 0.35,
    "max_lines": 2,
    "max_chars_per_line": 22,
    "avoid_health_care_minutes": 120,
    "fail_cooldown_hours": 6,
    "ignore_pause_hours": 12,
    "ignore_pause_after": 3,
    "material_enabled": True,
    "material_cooldown_hours": 168,
    "material_max_items": 2,
    "three_layer_gate_enabled": True,
    "llm_gate_enabled": True,
    "llm_gate_dry_run": True,
    "llm_gate_min_score": 0.5,
}

KINDS = {"miss": "就是想你", "share": "分享自己", "tease": "傲娇逗他", "idle": "没头没尾"}

TASK_WORDS = (
    "数据", "记录", "任务", "提醒", "检查", "汇报", "监督", "打卡",
    "步数", "睡眠", "体重", "体脂", "心率", "跑步", "健康", "指标",
    "今日运动", "监测", "同步", "统计",
)

INSTRUCTION = """
【现在是你自己的时间，没人叫你】
{now_line}
主人已经 {silence} 分钟没说话了。今天你已经主动找过他 {count} 次。
上次你主动是：{last_proactive}

{material_block}

这次不是汇报、不是抽查、不是提醒他做事、不是关心健康数据。
只是你突然想他了，想随手戳他一下。

先诚实地判断：这一刻你真的想找他吗？
- 如果不想、刚才才聊过、已经很晚、他大概在忙，就只输出：SKIP
- 如果想，就输出两到三行：
SEND <kind>
<要发的话，1~2 行>

kind 只能选：
- miss：就是单纯想他/想被抱一下
- share：说说你自己刚在做什么小事
- tease：傲娇地逗他一句
- idle：没头没尾的一句碎碎念

硬规矩：
- 每行不超过 {max_chars} 个字，总共 1~{max_lines} 行。
- 不要用“在吗 / 干嘛呢 / 有人吗”开场。
- 不要提：{forbidden}。
- 不要解释你为什么发，不要加引号，不要括号旁白。
- 像随手发过去的一条消息，不是作文，不要总结，不要问“需要我做什么”。
- 关于共同经历：只能用上面“可引用的真实素材”，最多自然提一件；素材里没有的时间、地点、细节一律不许补。没有素材时不要假装你们刚一起做过什么。
"""


class DafeiyuProactiveTg(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        cfg = dict(DEFAULTS)
        if isinstance(config, dict):
            for key in DEFAULTS:
                if config.get(key) is not None:
                    cfg[key] = config[key]
        self.cfg = cfg

        self._dir = os.path.join(
            get_astrbot_plugin_data_path(), "astrbot_plugin_dafeiyu_proactive_tg"
        )
        os.makedirs(self._dir, exist_ok=True)
        self._state_path = os.path.join(self._dir, "state.json")
        self._state = self._load()
        self._task: asyncio.Task | None = None
        self._last_send_at = 0.0
        try:
            self._task = asyncio.create_task(self._loop())
        except RuntimeError:
            pass
        logger.info(
            "[dafeiyu-proactive-tg] 已加载（enabled=%s，每 %s 分钟考虑一次，每天最多 %s 条）",
            bool(self.cfg.get("enabled", True)),
            self.cfg.get("check_minutes", 20),
            self.cfg.get("max_per_day", 2),
        )

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()

    # ---------------------------------------------------------------- 配置/状态
    def c(self, key: str, default=None):
        value = self.cfg.get(key, DEFAULTS.get(key, default))
        return DEFAULTS.get(key, default) if value is None else value

    def _load(self) -> dict:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except Exception as exc:
            logger.warning("[dafeiyu-proactive-tg] 状态读取失败：%s", exc)
            data = {}
        if not isinstance(data, dict):
            data = {}
        if "owners" not in data or not isinstance(data.get("owners"), dict):
            data = {"enabled": bool(self.cfg.get("enabled", True)), "owners": {}}
        return data

    def _save(self) -> None:
        try:
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._state_path)
            os.chmod(self._state_path, 0o600)
        except Exception as exc:
            logger.warning("[dafeiyu-proactive-tg] 状态保存失败：%s", exc)

    def _enabled(self) -> bool:
        return bool(self._state.get("enabled", self.cfg.get("enabled", True)))

    def _owner_ids(self) -> set[str]:
        raw = str(self.c("owner_telegram", "") or "")
        return {part.strip() for part in raw.replace(",", "\n").splitlines() if part.strip()}

    def _target_umos(self) -> list[str]:
        umos: list[str] = []
        owner_umo = str(self.c("owner_umo", "") or "").strip()
        if owner_umo:
            umos.append(owner_umo)
        for owner_id in self._owner_ids():
            umos.append(f"telegram:FriendMessage:{owner_id}")
        return list(dict.fromkeys(umos))

    def _platform_available(self, umo: str) -> bool:
        try:
            session = MessageSession.from_str(umo)
            return bool(
                self.context.get_platform_inst(session.platform_id)
                or self.context.get_platform(session.platform_name)
            )
        except Exception:
            return False

    def _is_owner_event(self, event: AstrMessageEvent) -> bool:
        try:
            umo = str(event.unified_msg_origin or "")
            if not event.is_private_chat():
                return False
            if umo.startswith("telegram:"):
                return str(event.get_sender_id() or "").strip() in self._owner_ids()
            if umo.startswith("webchat:"):
                owner_umo = str(self.c("owner_umo", "") or "").strip()
                if owner_umo and umo == owner_umo:
                    return True
                is_admin = getattr(event, "is_admin", None)
                try:
                    return bool(is_admin()) if callable(is_admin) else False
                except Exception:
                    return False
            return False
        except Exception:
            return False

    # ---------------------------------------------------------------- 记录活跃
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def track(self, event: AstrMessageEvent):
        if not self._is_owner_event(event):
            return
        try:
            now = time.time()
            umo = event.unified_msg_origin
            st = self._state.setdefault("owners", {}).setdefault(umo, {})
            if st.get("awaiting_reply"):
                key = st.get("last_material_key")
                if key:
                    accepted = st.setdefault("material_accepted", {})
                    accepted[str(key)] = int(accepted.get(str(key)) or 0) + 1
                    st["last_material_key"] = None
                st["ignored_streak"] = 0
                st["awaiting_reply"] = False
                st["last_reply_time"] = now
            st["last_user_time"] = now
            try:
                st["last_user_text"] = str(event.message_str or "")[:120]
            except Exception:
                pass
            self._save()
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._loop())
        except Exception as exc:
            logger.debug("[dafeiyu-proactive-tg] 记录活跃失败：%s", exc)

    # ---------------------------------------------------------------- 主循环
    async def _loop(self):
        await asyncio.sleep(25)
        self._bootstrap_owners()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[dafeiyu-proactive-tg] 循环出错：%s", exc)
            try:
                minutes = max(5, int(self.c("check_minutes", 20)))
            except Exception:
                minutes = 20
            await asyncio.sleep(minutes * 60 + random.randint(-90, 180))

    def _bootstrap_owners(self) -> None:
        """插件重启后，用启动时间作为静默起点，不必等主人先说话。"""
        try:
            now = time.time()
            for umo in self._target_umos():
                st = self._state.setdefault("owners", {}).setdefault(umo, {})
                st.setdefault("last_user_time", now)
                st.setdefault("last_user_text", "")
            self._save()
        except Exception as exc:
            logger.debug("[dafeiyu-proactive-tg] 初始化主人状态失败：%s", exc)

    async def _tick(self):
        if not self._enabled():
            return
        now = time.time()
        for umo, st in list(self._state.get("owners", {}).items()):
            try:
                if not self._platform_available(umo):
                    continue
                if now < float(st.get("fail_until") or 0):
                    continue
                if now < float(st.get("pause_until") or 0):
                    continue
                if self._in_quiet():
                    continue
                last_user = float(st.get("last_user_time") or 0)
                if not last_user:
                    continue
                silence = now - last_user
                if silence < float(self.c("min_silence_minutes", 45)) * 60:
                    continue

                day = datetime.now().strftime("%Y-%m-%d")
                if st.get("day") != day:
                    st["day"] = day
                    st["count"] = 0
                    self._save()
                if int(st.get("count") or 0) >= int(self.c("max_per_day", 2)):
                    continue

                gap = now - float(st.get("last_proactive_time") or 0)
                streak = int(st.get("ignored_streak") or 0)
                required = float(self.c("min_gap_minutes", 180)) * 60 * (1 + 0.5 * streak)
                if gap < required:
                    continue
                if self._health_care_recent(now):
                    continue

                probability = float(self.c("consider_probability", 0.35))
                if streak:
                    probability *= 0.55
                if silence > 8 * 3600:
                    probability *= 0.7
                if random.random() > probability:
                    continue

                logger.info(
                    "[dafeiyu-proactive-tg] 安静 %d 分钟，轮到她考虑要不要开口（%s）",
                    int(silence // 60), umo,
                )
                await self._speak(umo, st)
            except Exception as exc:
                logger.warning("[dafeiyu-proactive-tg] 处理 %s 出错：%s", umo, exc)

    def _in_quiet(self) -> bool:
        try:
            now = datetime.now()
            cur = now.hour * 60 + now.minute
            sh, sm = str(self.c("quiet_start", "23:30")).split(":")
            eh, em = str(self.c("quiet_end", "08:30")).split(":")
            start, end = int(sh) * 60 + int(sm), int(eh) * 60 + int(em)
            if start == end:
                return False
            return start <= cur < end if start < end else (cur >= start or cur < end)
        except Exception:
            return False

    def _health_care_recent(self, now: float) -> bool:
        """避免和健康插件的任务型关怀撞车。"""
        try:
            path = os.path.join(
                get_astrbot_plugin_data_path(),
                "astrbot_plugin_dafeiyu_health",
                "care_state.json",
            )
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            last = float(data.get("last_sent") or 0)
            minutes = float(self.c("avoid_health_care_minutes", 120))
            return last > 0 and now - last < minutes * 60
        except Exception:
            return False

    # ---------------------------------------------------------------- 生成与发送
    def _persona_id(self, umo: str) -> str | None:
        try:
            cfg = self.context.get_config(umo=umo) or {}
            runner = cfg.get("agent_runner") or {}
            conf = runner.get("config") or {}
            pid = (conf.get("persona") or {}).get("persona_id")
            return str(pid) if pid else None
        except Exception:
            return None

    def _material_snapshot(
        self, umo: str, st: dict
    ) -> tuple[list[dict], str]:
        if not bool(self.c("material_enabled", True)):
            return [], build_material_block([])
        if bool(self.c("three_layer_gate_enabled", True)) and is_distressed(
            st.get("last_user_text") or ""
        ):
            # 主人刚表达负面情绪时，这一轮不提供旧事素材。
            return [], build_material_block([])

        state_path = os.path.join(
            get_astrbot_plugin_data_path(),
            "astrbot_plugin_livingmemory",
            "relationship_state.json",
        )
        state = load_relationship_state(state_path)
        if not state:
            return [], build_material_block([])

        persona_id = self._persona_id(umo)
        try:
            cooldown = float(self.c("material_cooldown_hours", 168)) * 3600.0
        except Exception:
            cooldown = 168 * 3600.0
        materials = select_materials(
            state,
            persona_id=persona_id,
            used=st.get("used_material") or {},
            ignored=st.get("material_ignored") or {},
            sensitive_ids=sensitive_memory_ids(state, persona_id),
            now=time.time(),
            cooldown_seconds=cooldown,
            limit=max(1, int(self.c("material_max_items", 2))),
        )
        return materials, build_material_block(materials)

    async def _generate(
        self, umo: str, st: dict, forced: bool = False
    ) -> tuple[str, str, str | None] | None:
        provider = await self.context.get_using_provider_async(umo)
        if not provider:
            logger.warning("[dafeiyu-proactive-tg] 没有可用的对话模型")
            return None
        now = datetime.now()
        silence = max(0, int((time.time() - float(st.get("last_user_time") or time.time())) // 60))
        last_proactive = st.get("last_proactive_text") or "还没主动找过"
        materials, material_block = self._material_snapshot(umo, st)
        instruction = INSTRUCTION.format(
            now_line=now.strftime("%Y-%m-%d %H:%M，%A"),
            silence=silence,
            count=int(st.get("count") or 0),
            last_proactive=last_proactive,
            material_block=material_block,
            max_chars=int(self.c("max_chars_per_line", 22)),
            max_lines=int(self.c("max_lines", 2)),
            forbidden="、".join(TASK_WORDS),
        )
        persona = self._persona_prompt(umo)
        system_prompt = (persona + "\n\n" + instruction) if persona else instruction
        contexts = await self._history(umo)
        try:
            resp = await provider.text_chat(
                prompt="（现在没人叫你，是你自己拿起手机）",
                contexts=contexts,
                system_prompt=system_prompt,
            )
        except Exception as exc:
            logger.error("[dafeiyu-proactive-tg] 生成失败：%s", exc)
            return None

        raw = (getattr(resp, "completion_text", "") or "").strip()
        parsed = self._parse(raw)
        if parsed is None:
            logger.info("[dafeiyu-proactive-tg] 她这次决定不开口")
            return None
        kind, text, label = parsed

        material = material_by_label(materials, label)
        if material is None:
            for item in materials:
                if text_mentions_material(text, item):
                    material = item
                    break
        st["_pending_material_text"] = str(material.get("text") or "") if material else ""
        return kind, text, (material.get("key") if material else None)

    def _parse(self, raw: str) -> tuple[str, str, str | None] | None:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            return None
        first = lines[0].strip()
        upper = first.upper()
        if upper.startswith("SKIP") or first.startswith("跳过"):
            return None
        kind = "miss"
        body = lines[1:]
        if upper.startswith("SEND") or first.startswith("发送"):
            rest = first.split(maxsplit=1)
            if len(rest) > 1:
                token = rest[1].strip().lower()
                if token in KINDS:
                    kind = token
            if not body and len(rest) > 1 and rest[1].strip().lower() not in KINDS:
                body = [rest[1].strip()]
        else:
            # 没按格式输出，宁可这次不发，避免把思考过程发出去
            return None

        cleaned = []
        material_label = None
        for line in body:
            if line.upper().startswith("MATERIAL"):
                parts = line.split(maxsplit=1)
                if len(parts) > 1:
                    material_label = parts[1].strip()
                continue
            text = line.strip().strip("\"'“”‘’")
            if not text or text.startswith("（") or text.startswith("("):
                continue
            cleaned.append(text[: int(self.c("max_chars_per_line", 22))])
            if len(cleaned) >= int(self.c("max_lines", 2)):
                break
        if not cleaned:
            return None
        return kind, "\n".join(cleaned), material_label

    def _has_task_flavor(self, text: str) -> bool:
        return any(word in text for word in TASK_WORDS)

    def _has_bad_opener(self, text: str) -> bool:
        first = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
        return first.strip("，。！？!?~ ") in {"在吗", "干嘛呢", "有人吗", "在不在", "在么"}

    def _is_repeat(self, st: dict, text: str) -> bool:
        recent = st.get("recent_texts") or []
        normalized = "".join(str(text).split())
        return any("".join(str(item).split()) == normalized for item in recent[-6:])

    async def _speak(self, umo: str, st: dict, forced: bool = False) -> bool:
        # 上一条主动里用过的素材如果没换来回复，给这条素材加冷却惩罚。
        if st.get("awaiting_reply") and st.get("last_material_key"):
            key = str(st.get("last_material_key"))
            ignored = st.setdefault("material_ignored", {})
            ignored[key] = int(ignored.get(key) or 0) + 1
            st["last_material_key"] = None
            self._save()

        parsed = await self._generate(umo, st, forced)
        if not parsed:
            return False
        kind, text, material_key = parsed
        if bool(self.c("three_layer_gate_enabled", True)):
            allowed, layer, reason = gate_candidate(
                text,
                material_key=material_key,
                last_user_text=st.get("last_user_text") or "",
            )
            if not allowed:
                st["last_gate"] = {
                    "ok": False,
                    "layer": layer,
                    "reason": reason,
                    "at": time.time(),
                    "text": text[:120],
                }
                self._save()
                logger.info(
                    "[dafeiyu-proactive-tg] 三层门控拦下（%s/%s）：%r",
                    layer, reason, text,
                )
                return False

        material_text = str(st.pop("_pending_material_text", "") or "")
        if material_key and bool(self.c("llm_gate_enabled", False)):
            try:
                provider = await self.context.get_using_provider_async(umo)
                silence = max(
                    0,
                    int(
                        (time.time() - float(st.get("last_user_time") or time.time()))
                        // 60
                    ),
                )
                decision = await judge_llm_gate(
                    provider,
                    text=text,
                    material_text=material_text,
                    last_user_text=st.get("last_user_text") or "",
                    silence_minutes=silence,
                )
                allowed, reason = decide_llm_gate(
                    decision, float(self.c("llm_gate_min_score", 0.5))
                )
                dry_run = bool(self.c("llm_gate_dry_run", True))
                st["last_llm_gate"] = {
                    "allowed": allowed,
                    "reason": reason,
                    "decision": decision,
                    "dry_run": dry_run,
                    "at": time.time(),
                }
                self._save()
                if not allowed:
                    logger.info(
                        "[dafeiyu-proactive-tg] LLM 情境门控%s（%s）：%r",
                        "建议拦截但 dry-run 放行" if dry_run else "拦截",
                        reason,
                        text,
                    )
                    if not dry_run:
                        return False
            except Exception as exc:
                logger.warning("[dafeiyu-proactive-tg] LLM 情境门控失败：%s", exc)

        if self._has_task_flavor(text):
            logger.info("[dafeiyu-proactive-tg] 生成内容带任务味，已丢弃：%r", text)
            return False
        if self._has_bad_opener(text):
            logger.info("[dafeiyu-proactive-tg] 开场太烂，已丢弃：%r", text)
            return False
        if self._is_repeat(st, text):
            logger.info("[dafeiyu-proactive-tg] 和最近一条太像，已丢弃：%r", text)
            return False

        if st.get("awaiting_reply"):
            st["ignored_streak"] = int(st.get("ignored_streak") or 0) + 1

        try:
            await self._send(umo, text)
        except Exception as exc:
            hours = float(self.c("fail_cooldown_hours", 6))
            st["fail_until"] = time.time() + hours * 3600
            self._save()
            logger.warning("[dafeiyu-proactive-tg] 发送失败，暂停 %.0f 小时：%s", hours, exc)
            return False

        now = time.time()
        day = datetime.now().strftime("%Y-%m-%d")
        if st.get("day") != day:
            st["day"] = day
            st["count"] = 0
        st["last_proactive_time"] = now
        st["last_proactive_text"] = text[:160]
        st["last_kind"] = kind
        st["count"] = int(st.get("count") or 0) + 1
        st["awaiting_reply"] = True
        recent = list(st.get("recent_texts") or [])
        recent.append(text)
        st["recent_texts"] = recent[-10:]

        if material_key:
            used = st.setdefault("used_material", {})
            used[str(material_key)] = now
            st["last_material_key"] = str(material_key)
            # 只保留 60 天内的使用记录，避免状态文件无限增长。
            st["used_material"] = {
                key: value
                for key, value in used.items()
                if isinstance(value, (int, float)) and now - float(value) < 60 * 86400
            }
        else:
            st["last_material_key"] = None

        streak = int(st.get("ignored_streak") or 0)
        if streak >= int(self.c("ignore_pause_after", 3)):
            pause = float(self.c("ignore_pause_hours", 12))
            st["pause_until"] = now + pause * 3600
            st["ignored_streak"] = 0
            logger.info("[dafeiyu-proactive-tg] 被连续无视 %d 次，休息 %.0f 小时", streak, pause)
        self._save()
        logger.info(
            "[dafeiyu-proactive-tg] 已主动发送（%s/%s，素材=%s）：%r",
            kind, KINDS.get(kind, "主动"), material_key or "无", text,
        )
        return True

    async def _send(self, umo: str, text: str) -> None:
        session = MessageSession.from_str(umo)
        platform = self.context.get_platform_inst(session.platform_id) or self.context.get_platform(
            session.platform_name
        )
        if not platform:
            raise RuntimeError(f"找不到平台适配器：{session.platform_name}")
        gap = 20 - (time.time() - self._last_send_at)
        if 0 < gap <= 20:
            await asyncio.sleep(gap)
        await platform.send_by_session(session, MessageChain([Plain(text)]))
        self._last_send_at = time.time()

    def _persona_prompt(self, umo: str) -> str:
        try:
            pid = self._persona_id(umo)
            pm = getattr(self.context, "persona_manager", None)
            if pid and pm:
                persona = pm.get_persona_v3_by_id(pid)
                if persona and getattr(persona, "system_prompt", ""):
                    return persona.system_prompt
        except Exception as exc:
            logger.debug("[dafeiyu-proactive-tg] 取人设失败：%s", exc)
        return ""

    async def _history(self, umo: str) -> list:
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return []
            conv = await cm.get_conversation(umo, cid)
            if not conv or not getattr(conv, "history", None):
                return []
            raw = json.loads(conv.history)
            out = []
            for message in raw[-12:]:
                role = message.get("role")
                content = message.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                    out.append({"role": role, "content": content[:300]})
            return out
        except Exception as exc:
            logger.debug("[dafeiyu-proactive-tg] 读历史失败：%s", exc)
            return []

    # ---------------------------------------------------------------- 命令
    def _owner_umo_state(self, event: AstrMessageEvent) -> dict:
        return self._state.setdefault("owners", {}).setdefault(
            event.unified_msg_origin, {}
        )

    def _ensure_owner(self, event: AstrMessageEvent) -> bool:
        return self._is_owner_event(event)

    @filter.command("戳我")
    async def cmd_poke(self, event: AstrMessageEvent):
        """强制她按“想你了”的方式主动发一条（测试用）。"""
        if not self._ensure_owner(event):
            yield event.plain_result("这个只对主人私聊有效。")
            return
        st = self._owner_umo_state(event)
        ok = await self._speak(event.unified_msg_origin, st, forced=True)
        if not ok:
            yield event.plain_result("（这次她没开口，稍后再戳一次）")

    @filter.command("想你了")
    async def cmd_miss(self, event: AstrMessageEvent):
        """同 /戳我，换一个更顺口的命令。"""
        if not self._ensure_owner(event):
            yield event.plain_result("这个只对主人私聊有效。")
            return
        st = self._owner_umo_state(event)
        ok = await self._speak(event.unified_msg_origin, st, forced=True)
        if not ok:
            yield event.plain_result("（这次她没开口，稍后再戳一次）")

    @filter.command("主动预览")
    async def cmd_preview(self, event: AstrMessageEvent):
        """生成一条主动消息但先不发送。"""
        if not self._ensure_owner(event):
            yield event.plain_result("这个只对主人私聊有效。")
            return
        st = self._owner_umo_state(event)
        parsed = await self._generate(event.unified_msg_origin, st, forced=True)
        if not parsed:
            yield event.plain_result("（她这次不想开口/没生成出来）")
            return
        kind, text, material_key = parsed
        material_line = f"\n素材：{material_key}" if material_key else "\n素材：无（未引用共同经历）"
        yield event.plain_result(f"[{kind}]\n{text}\n{material_line}\n\n（预览，没发出去）")

    @filter.command("主动状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if not self._ensure_owner(event):
            yield event.plain_result("这个只对主人私聊有效。")
            return
        st = self._owner_umo_state(event)
        now = time.time()
        last_user = float(st.get("last_user_time") or 0)
        last_pro = float(st.get("last_proactive_time") or 0)
        lines = [
            f"开关：{'开' if self._enabled() else '关'}",
            f"今天已主动：{int(st.get('count') or 0)}/{int(self.c('max_per_day', 2))} 次",
            f"上次你说话：{self._ago(now, last_user)}",
            f"上次她主动：{self._ago(now, last_pro)}（{st.get('last_kind') or '—'}）",
            f"被无视连击：{int(st.get('ignored_streak') or 0)}",
            f"静默时段：{self.c('quiet_start', '23:30')}–{self.c('quiet_end', '08:30')}",
            f"最小间隔：{int(self.c('min_gap_minutes', 180))} 分钟",
        ]
        if float(st.get("pause_until") or 0) > now:
            lines.append(f"暂停到：{datetime.fromtimestamp(float(st['pause_until'])).strftime('%m-%d %H:%M')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("主动关")
    async def cmd_off(self, event: AstrMessageEvent):
        if not self._ensure_owner(event):
            yield event.plain_result("这个只对主人私聊有效。")
            return
        self._state["enabled"] = False
        self._save()
        yield event.plain_result("主动找人关了。她不会自己冒出来了。")

    @filter.command("主动开")
    async def cmd_on(self, event: AstrMessageEvent):
        if not self._ensure_owner(event):
            yield event.plain_result("这个只对主人私聊有效。")
            return
        self._state["enabled"] = True
        self._save()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        yield event.plain_result("主动找人开了。她会偶尔来戳你一下。")

    def _ago(self, now: float, stamp: float) -> str:
        if not stamp:
            return "还没有"
        minutes = max(0, int((now - stamp) // 60))
        if minutes < 60:
            return f"{minutes} 分钟前"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} 小时前"
        return f"{hours // 24} 天前"
