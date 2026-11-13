"""大肥鱼换声（控制台驱动）

音色选择、试听、设计都在「大肥鱼控制台」的「音色」/「音色实验室」里完成。
这个插件只做两件事：

1. 每次 LLM 请求前读 voice_state.json，把当前 Breeze 引擎的 voice 换成控制台选的；
2. 接管 Breeze provider 的 get_audio：带着当前音色的 instruction（实验室里那段 Prompt）
   去请求 Breeze，保证 Q8 / F16 日常聊天都能保持设计时的语气，而不是退回默认念法。

不在聊天里注册任何换声工具/命令。
"""

import asyncio
import json
import os
import time
import uuid
import wave

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path, get_astrbot_temp_path

PLUGIN_NAME = "astrbot_plugin_dafeiyu_voice_switch"

TAIL_SILENCE_SECONDS = 1.0


def ensure_tail_silence(path: str, seconds: float = TAIL_SILENCE_SECONDS) -> str:
    """在 WAV 末尾补一秒静音，避免 QQ 播放/SILK 解码把最后一个字提前收口。"""
    if seconds <= 0 or not path or not path.lower().endswith(".wav"):
        return path
    if not os.path.isfile(path):
        return path
    try:
        with wave.open(path, "rb") as r:
            params = r.getparams()
            frames = r.readframes(params.nframes)
        rate = params.framerate or 24000
        channels = params.nchannels or 1
        width = params.sampwidth or 2
        frame_size = max(1, channels * width)
        pad = int(rate * float(seconds)) * frame_size
        tail_len = min(pad, len(frames))
        if tail_len > 0 and not any(frames[-tail_len:]):
            return path  # 已经垫过
        out = os.path.splitext(path)[0] + "_tail.wav"
        with wave.open(out, "wb") as w:
            w.setparams(params)
            w.writeframes(frames + b"\x00" * pad)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[dafeiyu-voice-switch] 补尾部静音失败，继续用原文件: {e}")
        return path


class DafeiyuVoiceSwitch(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.conf = config
        self.default = str((self.conf.get("default_voice") if self.conf else "") or "dafeiyu_mihu").strip()
        self.state_path = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME, "voice_state.json")
        self._voice = str(self._load_state().get("voice") or self.default)
        self._applied = False
        self._base = "http://127.0.0.1:8137"

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _breeze_inst(self):
        pm = getattr(self.context, "provider_manager", None)
        if not pm:
            return None
        seen = set()
        cands = list(getattr(pm, "tts_provider_insts", []) or [])
        cands += list((getattr(pm, "inst_map", {}) or {}).values())
        for inst in cands:
            if inst is None or id(inst) in seen:
                continue
            seen.add(id(inst))
            cfg = getattr(inst, "provider_config", {}) or {}
            api = str(cfg.get("api_base") or "")
            pid = str(cfg.get("id") or "")
            if "8137" in api or "breeze" in pid.lower() or "Breeze" in pid:
                return inst
        return None

    def _install_tts(self, inst) -> None:
        """把 Breeze provider 的 get_audio 换成带 instruction 的实现。"""
        cfg = getattr(inst, "provider_config", {}) or {}
        base = str(cfg.get("api_base") or "http://127.0.0.1:8137/v1").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        self._base = base or "http://127.0.0.1:8137"

        if getattr(inst, "_dafeiyu_tts_wrapped", False):
            return

        async def get_audio(text: str) -> str:
            return await self._tts(text)

        inst.get_audio = get_audio
        inst._dafeiyu_tts_wrapped = True
        logger.info(f"[dafeiyu-voice-switch] 已接管 Breeze TTS：{self._base}")

    async def _tts(self, text: str) -> str:
        st = self._load_state()
        voice = str(st.get("voice") or self.default).strip() or self.default
        instruction = str(st.get("instruction") or "").strip()
        pcm = b""
        rate = 24000
        for attempt in range(5):
            form = aiohttp.FormData()
            form.add_field("text", text)
            form.add_field("voice_id", voice)
            if instruction:
                form.add_field("instruction", instruction)
            form.add_field("cfg_scale", "1.0")
            form.add_field("seed", "42")
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        self._base + "/v1/audio/speech", data=form,
                        timeout=aiohttp.ClientTimeout(total=600, connect=5),
                    ) as r:
                        if r.status == 409:  # Breeze 一次只跑一个
                            await asyncio.sleep(1.5 + attempt)
                            continue
                        if r.status != 200:
                            body = (await r.text())[:160]
                            raise RuntimeError(f"Breeze HTTP {r.status}: {body}")
                        pcm = await r.read()
                        rate = int(r.headers.get("X-Sample-Rate", "24000"))
                        break
            except Exception as e:  # noqa: BLE001
                # 连接失败（服务没起/端口被拒）重试没有意义：Windows 上连一个
                # 没人监听的本地端口每次要等约 2 秒，重试 5 次会把一条回复拖 16 秒，
                # 而这条回复最后还是要退回打字。直接认输，让上层立刻改发文字。
                if isinstance(e, (aiohttp.ClientConnectorError, ConnectionError, OSError)):
                    raise RuntimeError(f"Breeze TTS 失败（服务没起）: {e}")
                if attempt == 4:
                    raise RuntimeError(f"Breeze TTS 失败: {e}")
                await asyncio.sleep(1.5)
        if not pcm:
            raise RuntimeError("Breeze TTS 没有返回音频")

        out = os.path.join(get_astrbot_temp_path(),
                           f"dafeiyu_tts_{uuid.uuid4().hex[:10]}.wav")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if pcm[:4] == b"RIFF":
            with open(out, "wb") as f:
                f.write(pcm)
        else:
            with wave.open(out, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate or 24000)
                w.writeframes(pcm)
        return ensure_tail_silence(out)

    def _apply(self, voice: str) -> bool:
        inst = self._breeze_inst()
        if not inst:
            return False
        self._install_tts(inst)
        try:
            inst.voice = voice
        except Exception:  # noqa: BLE001
            pass
        try:
            inst.provider_config["openai-tts-voice"] = voice
        except Exception:  # noqa: BLE001
            pass
        return True

    async def initialize(self):
        """插件加载完就接管 TTS，别等第一条聊天。"""
        name = str(self._load_state().get("voice") or self.default).strip()
        if self._apply(name):
            self._voice = name
            self._applied = True
            logger.info(f"[dafeiyu-voice-switch] 启动时接管 TTS，音色：{name}")

    @filter.on_llm_request()
    async def _hook(self, event: AstrMessageEvent, req):
        state = self._load_state()
        name = str(state.get("voice") or self.default).strip()
        if self._applied and name == self._voice:
            return
        if self._apply(name):
            self._voice = name
            self._applied = True
            logger.info(
                f"[dafeiyu-voice-switch] 应用控制台音色：{name}"
                f"{'（带 instruction）' if state.get('instruction') else ''}"
            )
