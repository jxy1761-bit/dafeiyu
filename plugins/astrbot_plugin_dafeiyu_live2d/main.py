"""大肥鱼 · 桌宠联动（PC 本地 / 手机镜像）

把 AstrBot 的最终回复投给 PC 上的 Live2D 桌宠（whale-pet-shell，127.0.0.1:61943）。

两条路，自动选：
  1. 本机桌宠在线   -> 直接 POST /say
  2. 本机没有桌宠   -> 若配置了 peer_url，就镜像到 PC 的中转（默认 :61944，带 token）

手机上的 Telegram 轮询器走第 2 条；PC 上的 webchat / Telegram 走第 1 条。
连续 5 分钟连不上 PC（或本机桌宠）就自动暂停镜像，之后用 /pet_connect 手动恢复。

不阻塞聊天：发送全部放在后台任务里，失败只写日志。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_dafeiyu_live2d"

GESTURES = {"hand_on_chest", "point", "grass"}

GES_RE = re.compile(r"\[gesture:\s*([a-z_]+)\s*\]", re.I)
PERFORM_RE = re.compile(r"\[perform:\s*([^\]]+)\]", re.I)
PERF_EXPRS = {"爱心眼", "脸红", "流泪", "黑脸", "＞＜", "圈圈眼", "屑屑眼", "兔牙", "流冷汗", "加载", "前倾"}
PERF_GESTURES = {"hand_on_chest", "point", "grass"}
PERF_CUES = [
    ("expr=爱心眼, tilt=left, gesture=hand_on_chest", r"爱你|喜欢你|最喜欢|抱抱|亲亲|贴贴|想你了|么么"),
    ("expr=脸红, tilt=left, gesture=hand_on_chest", r"害羞|脸红|不好意思|别看我|羞"),
    ("expr=兔牙, nod=2", r"哈哈|嘿嘿|嘻嘻|好耶|太好了|开心|高兴|笑死|诶嘿"),
    ("expr=流泪, look=down, tilt=left", r"呜呜|想哭|难过|委屈|伤心|失落|眼泪|哭"),
    ("expr=黑脸, shake=2", r"生气|讨厌|坏蛋|过分|欺负|不理你"),
    ("expr=屑屑眼, nod=1", r"哼哼|得意|骄傲|当然|那是|才不|哼"),
    ("expr=圈圈眼, tilt=right", r"搞不懂|什么鬼|为什么|怎么会|看不懂|晕|咦|诶"),
    ("expr=＞＜, look=down", r"吓|惊|真的吗|不会吧|居然|天哪|困|睡觉|晚安|累了"),
    ("expr=流冷汗, tilt=left", r"尴尬|无语|糟糕|完蛋"),
]
PERFORM_HINT = """【桌宠小表演】
每次回复前必须先调用一次 pet_perform 工具，为这句话选 1~3 个动作，然后再写正文。
expr 可选：爱心眼/脸红/流泪/黑脸/＞＜/圈圈眼/屑屑眼/兔牙/流冷汗/加载/前倾；
head 可选：look:left/right/up/down、nod:1-4、shake:1-4、tilt:left/right（只选一个）；
gesture 可选：hand_on_chest/point/grass；smile 0-1；blink 1-4。
和这句话真实情绪一致，不要换装、改变体型、走路、跑步或转身。工具调用不要向用户解释。"""
VTS_HINT = """【VTS 桌宠】
你可以调用 vts_face(name) 切换她的脸部表情，或 vts_action(name) 触发热键动作（换装、手势等）。
有明显情绪或动作意图时，挑一个贴切的调用一次；不确定有哪些动作时先调用 vts_actions。不要每句都强行调用。"""
MD_CODE = re.compile(r"```.*?```", re.S)
MD_IMG = re.compile(r"!\[[^\]]*]\([^)]*\)")
MD_LINK = re.compile(r"\[([^\]]+)]\([^)]*\)")
URL = re.compile(r"https?://\S+")
MD_MARKS = re.compile(r"[*_`#>~]+")


def perform_spec_from_text(text: str) -> str:
    t = str(text or "")
    for spec, pattern in PERF_CUES:
        if re.search(pattern, t):
            return spec
    return "tilt=left"


def parse_perform(spec: str) -> list[dict]:
    """把 [perform: expr=脸红, tilt=left] 变成鲸语舞台能吃的 moves。"""
    opts = {}
    for part in str(spec or "").split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k and v:
            opts[k] = v
    actions = []
    expr = opts.get("expr") or opts.get("expression") or ""
    if expr in PERF_EXPRS:
        actions.append({"kind": "expression", "name": expr, "strength": 1,
                        "layer": "exclusive", "at": 0.15, "duration": 2.6})
    head = None
    if opts.get("look") in ("left", "right", "up", "down", "center"):
        head = {"kind": "look", "direction": opts["look"], "at": 0.2, "duration": 2.2, "strength": 0.92}
    elif opts.get("nod"):
        try: n = max(1, min(4, int(opts["nod"])))
        except Exception: n = 1
        head = {"kind": "nod", "count": n, "at": 0.2, "duration": 2.2, "strength": 0.95}
    elif opts.get("shake"):
        try: n = max(1, min(4, int(opts["shake"])))
        except Exception: n = 1
        head = {"kind": "shake_head", "count": n, "at": 0.2, "duration": 2.2, "strength": 0.95}
    elif opts.get("tilt") in ("left", "right"):
        head = {"kind": "tilt", "direction": opts["tilt"], "at": 0.2, "duration": 2.2, "strength": 0.92}
    if head:
        actions.append(head)
    if not expr and opts.get("smile"):
        try: s = max(0.55, min(1.0, float(opts["smile"])))
        except Exception: s = 0.75
        actions.append({"kind": "smile", "at": 0.4, "duration": 2.2, "strength": s})
    if opts.get("blink"):
        try: n = max(1, min(4, int(opts["blink"])))
        except Exception: n = 1
        actions.append({"kind": "blink", "side": "both", "count": n, "at": 1.8, "duration": 0.5})
    if opts.get("gesture") in PERF_GESTURES:
        actions.append({"kind": "gesture", "name": opts["gesture"], "at": 0.3, "duration": 7.0})
    if not actions:
        actions.append({"kind": "tilt", "direction": "left", "at": 0.2, "duration": 2.4, "strength": 0.72})
    return actions


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for i in range(len(cut) - 1, max(0, len(cut) - 30), -1):
        if cut[i] in "。！？!?…；;，,":
            return cut[: i + 1]
    return cut.rstrip() + "…"


class DafeiyuLive2D(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.conf = config
        self.state_path = os.path.join(
            get_astrbot_plugin_data_path(), PLUGIN_NAME, "state.json"
        )
        self.state = self._load_state()
        self._lock = asyncio.Lock()
        self._vts_used_at = 0.0
        self._perform = None
        self._perform_at = 0.0

    # ---------- 配置 / 状态 ----------

    def c(self, key: str, default=None):
        try:
            v = self.conf.get(key, default) if self.conf else default
        except Exception:
            v = default
        return default if v is None else v

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _url(self, key: str, default: str) -> str:
        return str(self.c(key, default) or default).rstrip("/")

    def _peer_enabled(self) -> bool:
        return bool(str(self.c("peer_url", "") or "").strip())

    # ---------- HTTP ----------

    async def _post(self, url: str, payload: dict, token: str = "", timeout: float = 3.0):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Pet-Token"] = token
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as s:
                async with s.post(url, json=payload, headers=headers) as r:
                    raw = await r.read()
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except Exception:
                        body = {"ok": False, "error": raw[:100].decode("utf-8", "replace")}
                    return r.status, body
        except Exception as e:  # noqa: BLE001
            return 0, {"ok": False, "error": str(e)[:160]}

    async def _get(self, url: str, token: str = "", timeout: float = 2.0):
        headers = {"X-Pet-Token": token} if token else {}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as s:
                async with s.get(url, headers=headers) as r:
                    raw = await r.read()
                    try:
                        return r.status, json.loads(raw.decode("utf-8"))
                    except Exception:
                        return r.status, {}
        except Exception as e:  # noqa: BLE001
            return 0, {"ok": False, "error": str(e)[:160]}

    async def _send_local(self, payload: dict):
        url = self._url("local_url", "http://127.0.0.1:61943") + "/say"
        return await self._post(url, payload, timeout=float(self.c("local_timeout", 2.5)))

    async def _send_peer(self, payload: dict):
        base = self._url("peer_url", "")
        if not base:
            return 0, {"ok": False, "error": "no peer_url"}
        token = str(self.c("peer_token", "") or "")
        return await self._post(base + "/pet/say", payload, token=token, timeout=float(self.c("peer_timeout", 4.0)))

    # ---------- VTS Action Bridge ----------

    def _vts_base(self) -> str:
        explicit = str(self.c("vts_bridge_url", "") or "").strip().rstrip("/")
        if explicit:
            return explicit if explicit.endswith("/vts") else explicit + "/vts"
        peer = self._url("peer_url", "")
        if peer:
            from urllib.parse import urlsplit, urlunsplit
            p = urlsplit(peer)
            return urlunsplit((p.scheme, p.netloc, "/vts", "", ""))
        return "http://127.0.0.1:61946/vts"

    def _vts_token(self) -> str:
        return str(self.c("peer_token", "") or "")

    async def _vts_call(self, path: str, payload: dict | None = None, timeout: float = 10.0):
        if not self.c("vts_enabled", False):
            return 0, {"ok": False, "error": "VTS 工具已在插件配置里关闭"}
        url = self._vts_base() + path
        token = self._vts_token()
        if payload is None:
            return await self._get(url, token=token, timeout=timeout)
        return await self._post(url, payload, token=token, timeout=timeout)

    VTS_FACE_CUES = [
        ("爱心眼", r"爱你|喜欢你|最喜欢|抱抱|亲亲|贴贴|想你了|么么"),
        ("脸红", r"害羞|脸红|不好意思|别看我|羞"),
        ("兔牙", r"哈哈|嘿嘿|嘻嘻|好耶|太好了|开心|高兴|笑死|诶嘿"),
        ("流泪", r"呜呜|想哭|难过|委屈|伤心|失落|眼泪|哭"),
        ("黑脸", r"生气|讨厌|坏蛋|过分|欺负|不理你"),
        ("屑屑眼", r"哼哼|得意|骄傲|当然|那是|才不|哼"),
        ("圈圈眼", r"搞不懂|什么鬼|为什么|怎么会|看不懂|晕|咦|诶"),
        ("＞＜", r"吓|惊|真的吗|不会吧|居然|困|睡觉|晚安|累了"),
        ("流冷汗", r"尴尬|无语|糟糕|完蛋|汗"),
    ]

    async def _vts_auto_face(self, text: str):
        """回复后自动选一张脸，让 VTS 有反馈；LLM 已经调用过工具时不抢。"""
        if not self.c("vts_enabled", False) or not self.c("vts_auto", False):
            return
        if time.time() - self._vts_used_at < 8:
            return
        t = text or ""
        for name, pattern in self.VTS_FACE_CUES:
            if re.search(pattern, t):
                status, body = await self._vts_call("/face", {"name": name}, timeout=8)
                if status != 200:
                    logger.debug(f"[live2d] VTS auto face failed: {body.get('error') or status}")
                return



    # ---------- 核心投递 ----------

    async def _push(self, text: str, voice: bool, gesture: str | None = None, moves: list | None = None):
        if not self.c("enabled", True):
            return
        try:
            text = _clip(text, int(self.c("max_chars", 260)))
            if not text:
                return
            payload = {
                "text": text,
                "gesture": gesture or "",
                "moves": moves or [],
                "voice": bool(voice),
                "early": True,
                # 壳子静音/没配 TTS 时会用这个估时长来铺动作，不能只在 voice=False 时给
                "ms": max(3200, min(12000, len(text) * 190)),
            }

            # 1) 本机桌宠
            status, body = await self._send_local(payload)
            if status == 200:
                await self._retry_busy(self._send_local, payload, body)
                self.state.update({"paused": False, "fail_since": 0, "last_ok": time.time()})
                self._save_state()
                return

            # 2) 镜像到 PC
            if not self.c("mirror", True) or not self._peer_enabled():
                return
            async with self._lock:
                if self.state.get("paused"):
                    return
                status, body = await self._send_peer(payload)
                if status == 200:
                    await self._retry_busy(self._send_peer, payload, body)
                    self.state.update({"paused": False, "fail_since": 0, "last_ok": time.time(),
                                       "last_error": ""})
                    self._save_state()
                    return
                now = time.time()
                if not self.state.get("fail_since"):
                    self.state["fail_since"] = now
                self.state["last_error"] = str(body.get("error") or status)[:200]
                give_up = max(1, int(self.c("give_up_minutes", 5))) * 60
                if now - float(self.state.get("fail_since") or now) >= give_up:
                    self.state["paused"] = True
                    logger.warning(
                        "[live2d] PC 中转连续 %s 分钟不通，已暂停镜像；回家后发 /pet_connect 恢复"
                        % int(give_up / 60)
                    )
                self._save_state()
        except Exception as e:  # noqa: BLE001
            logger.info(f"[live2d] push failed: {e}")

    async def _retry_busy(self, fn, payload: dict, body: dict):
        # 桌宠正在说上一句时 /say 会回 busy；等它说完再补，最多 4 次
        for _ in range(4):
            if not isinstance(body, dict) or not body.get("busy"):
                return
            await asyncio.sleep(1.2)
            status, body = await fn(payload)

    def _extract(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return None
        try:
            if not result.is_llm_result():
                return None
        except Exception:  # noqa: BLE001
            pass

        text = "".join(
            comp.text for comp in result.chain
            if isinstance(comp, Plain) and getattr(comp, "text", None)
        ).strip()
        has_voice = any(isinstance(c, Record) for c in result.chain)
        if not text:
            # 整段语音模式下正文被换成了 Record，用 LLM 原文兜底
            text = str(event.get_extra("live2d_llm_text") or "").strip()
        if not text:
            return None

        ges = None
        m = GES_RE.search(text)
        if m and m.group(1).lower() in GESTURES:
            ges = m.group(1).lower()
        text = GES_RE.sub("", text)

        moves = []
        if self._perform and time.time() - self._perform_at < 30:
            moves = self._perform
            self._perform = None
            self._perform_at = 0.0
        elif self.c("auto_perform", True):
            moves = parse_perform(perform_spec_from_text(text))
        text = PERFORM_RE.sub("", text)   # 兼容旧标记

        text = MD_CODE.sub("", text)
        text = MD_IMG.sub("", text)
        text = MD_LINK.sub(r"\1", text)
        text = URL.sub("", text)
        text = MD_MARKS.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return None
        return text, has_voice, ges, moves

    # ---------- 钩子 ----------

    @filter.on_llm_request()
    async def inject_hints(self, event: AstrMessageEvent, req):
        try:
            cur = getattr(req, "system_prompt", "") or ""
            if self.c("vts_enabled", False) and "【VTS 桌宠】" not in cur:
                cur = f"{cur.rstrip()}\n\n{VTS_HINT}".strip()
            if self.c("auto_perform", True) and "【每次回复的小表演】" not in cur:
                cur = f"{cur.rstrip()}\n\n{PERFORM_HINT}".strip()
            req.system_prompt = cur
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[live2d] 注入桌宠提示失败: {e}")

    def _strip_markers(self, event: AstrMessageEvent):
        """手势标记只给桌宠看，别跟着聊天正文发出去。"""
        try:
            result = event.get_result()
            if not result or not getattr(result, "chain", None):
                return
            for comp in result.chain:
                if isinstance(comp, Plain) and getattr(comp, "text", None):
                    t = PERFORM_RE.sub("", GES_RE.sub("", comp.text))
                    comp.text = re.sub(r"[ \t]+\n", "\n", t).strip()
        except Exception:  # noqa: BLE001
            pass

    @filter.on_llm_response()
    async def remember_llm_text(self, event: AstrMessageEvent, resp):
        try:
            t = getattr(resp, "completion_text", "") or ""
            if t:
                event.set_extra("live2d_llm_text", t)
        except Exception:  # noqa: BLE001
            pass

    @filter.on_decorating_result()
    async def to_pet(self, event: AstrMessageEvent):
        try:
            got = self._extract(event)
            # 标记一律从正文删掉（群聊不投桌宠也要删，否则 [gesture:xxx] 会露出来）
            self._strip_markers(event)
            if self.c("only_private", True):
                try:
                    if event.get_group_id():
                        return
                except Exception:  # noqa: BLE001
                    pass
            if not got:
                return
            text, has_voice, ges, moves = got
            # 已经发语音条时，桌宠只出气泡，避免同一句被念两遍
            voice = bool(self.c("voice", True)) and not has_voice
            asyncio.create_task(self._push(text, voice, ges, moves))
            if self.c("vts_auto", False):
                asyncio.create_task(self._vts_auto_face(text))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[live2d] hook failed: {e}")

    # ---------- 命令 ----------

    @filter.llm_tool(name="pet_perform")
    async def pet_perform(self, event: AstrMessageEvent, expr: str = "", head: str = "",
                          gesture: str = "", smile: float | None = None,
                          blink: int | None = None) -> str:
        """Record a small Live2D performance for the current reply. Call once before answering.

        Args:
            expr(string): Face expression: 爱心眼/脸红/流泪/黑脸/＞＜/圈圈眼/屑屑眼/兔牙/流冷汗/加载/前倾.
            head(string): One head action, e.g. look:left, look:down, nod:2, shake:2, tilt:left.
            gesture(string): One gesture: hand_on_chest, point, grass.
            smile(number): Smile strength 0..1, only when expr is empty.
            blink(number): Blink count 1..4.
        """
        parts = []
        if expr:
            parts.append("expr=" + str(expr).strip())
        h = str(head or "").strip().replace("：", ":")
        if ":" in h:
            k, v = h.split(":", 1)
            k = k.strip().lower()
            if k in ("look", "nod", "shake", "tilt"):
                parts.append(k + "=" + v.strip())
        if gesture:
            parts.append("gesture=" + str(gesture).strip())
        if smile is not None:
            parts.append("smile=" + str(smile))
        if blink is not None:
            parts.append("blink=" + str(blink))
        self._perform = parse_perform(", ".join(parts))
        self._perform_at = time.time()
        return "表演已记录：" + "、".join(a.get("kind", "") for a in self._perform)

    @filter.llm_tool(name="vts_actions")
    async def vts_actions(self, event: AstrMessageEvent) -> str:
        """List available VTube Studio action/hotkey names and face expressions.

        Use this before triggering an action if you are unsure what is available.
        """
        status, body = await self._vts_call("/hotkeys", None, timeout=8)
        if status != 200:
            return f"VTS 动作桥不可用（{self._vts_base()}）：{body.get('error') or status}"
        names = [h.get("name") or h.get("id") for h in body.get("hotkeys") or []]
        status2, body2 = await self._vts_call("/expressions", None, timeout=8)
        faces = [e.get("name") for e in (body2.get("expressions") or []) if e.get("name")]
        return "VTS 热键/动作：" + ("、".join(names) if names else "（无）") + \
               "\nVTS 表情：" + ("、".join(faces) if faces else "（无）")

    @filter.llm_tool(name="vts_action")
    async def vts_action(self, event: AstrMessageEvent, name: str) -> str:
        """Trigger a VTube Studio hotkey/action by its exact name.

        Args:
            name(string): Hotkey/action name from vts_actions, e.g. 爱心眼, 裙长切换, 抚胸手打开.
        """
        status, body = await self._vts_call("/hotkey", {"id": str(name or "").strip()}, timeout=12)
        if status != 200:
            return f"动作「{name}」没触发（{self._vts_base()}）：{body.get('error') or status}"
        self._vts_used_at = time.time()
        h = body.get("hotkey") or {}
        return f"已触发动作：{h.get('name') or name}"

    @filter.llm_tool(name="vts_face")
    async def vts_face(self, event: AstrMessageEvent, name: str) -> str:
        """Switch her face expression exclusively; clothes/accessories are not changed.

        Args:
            name(string): Face expression name, e.g. 脸红, 爱心眼, 圈圈眼, 黑脸, ＞＜.
        """
        status, body = await self._vts_call("/face", {"name": str(name or "").strip()}, timeout=12)
        if status != 200:
            return f"表情「{name}」没切换（{self._vts_base()}）：{body.get('error') or status}"
        self._vts_used_at = time.time()
        e = body.get("expression") or {}
        return f"表情已切换：{e.get('name') or name}"

    @filter.command("pet_status", alias={"桌宠状态"})
    async def pet_status(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有主人能看桌宠状态。")
            return
        local = self._url("local_url", "http://127.0.0.1:61943")
        ls, _ = await self._get(local + "/status")
        peer = self._url("peer_url", "")
        ps, pb = (0, {}) if not peer else await self._get(peer + "/health")
        lines = [
            f"本机桌宠：{'在线' if ls == 200 else '不通'}（{local}）",
            f"PC 中转：{'在线' if ps == 200 and pb.get('pet') else '不通' if peer else '未配置'}",
            f"镜像：{'开启' if self.c('mirror', True) else '关闭'}"
            + ("（已暂停，发 /pet_connect 恢复）" if self.state.get("paused") else ""),
            f"上次成功：{time.strftime('%m-%d %H:%M', time.localtime(self.state.get('last_ok') or 0)) if self.state.get('last_ok') else '还没成功过'}",
        ]
        if self.state.get("last_error"):
            lines.append(f"上次错误：{self.state['last_error']}")
        yield event.plain_result("\n".join(lines))

    @filter.command("pet_connect", alias={"桌宠连接"})
    async def pet_connect(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有主人能连桌宠。")
            return
        self.state.update({"paused": False, "fail_since": 0, "last_error": ""})
        self._save_state()
        local = self._url("local_url", "http://127.0.0.1:61943")
        ls, _ = await self._get(local + "/status")
        if ls == 200:
            yield event.plain_result("桌宠连接已重置：本机桌宠在线。")
            return
        peer = self._url("peer_url", "")
        if not peer:
            yield event.plain_result("桌宠连接已重置，但没有配置 PC 中转地址。")
            return
        ps, pb = await self._get(peer + "/health", token=str(self.c("peer_token", "") or ""), timeout=4)
        if ps == 200 and pb.get("pet"):
            yield event.plain_result("桌宠连接已重置：PC 中转在线，接下来会镜像过去。")
        elif ps == 200:
            yield event.plain_result("PC 中转在线，但 PC 上的桌宠没开；先启动桌宠再发一次。")
        else:
            yield event.plain_result("PC 还是不通：确认电脑开着、同一 Wi-Fi、61944 防火墙已放行。")

    @filter.command("pet_pause", alias={"桌宠断开"})
    async def pet_pause(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有主人能断开桌宠。")
            return
        self.state["paused"] = True
        self._save_state()
        yield event.plain_result("桌宠镜像已暂停；发 /pet_connect 恢复。")
