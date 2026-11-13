"""大肥鱼·自主语音

平台自带的语音是「掷骰子」：provider_tts_settings.trigger_probability 决定这条回复要不要
转成语音，模型既不知道、也控制不了。本插件把决定权交回给她。

两种用法：
  1. send_voice(text) —— 她挑一句话发语音条，最长可念约一分钟（超了就自动拆成几条连着发）。
  2. 整段语音模式 —— 开了之后，她的每一条文字回复都会自动转成语音条发出。
     **只由「大肥鱼控制台」里的手动开关控制**：没有 llm 工具、也没有关键词识别，
     聊天里说「用语音吧」「别发语音了」都不会改变状态（2026-11-09 用户要求全手动）。
     状态写在 voice_mode.json 的 _panel 总开关里，控制台和插件共用这一份。

单条语音的时长上限由 max_seconds 控制（默认 58 秒，QQ 语音上限 60 秒，留 2 秒余量）。
文字按标点切成一段段，逐段合成；每段真实时长用 WAV 头算出来，超时就对半再切一次。
合成出来的 WAV 用 silk-python 转成 QQ 原生 SILK（一分钟只要一两百 KB），失败就退回原文件。
"""

import asyncio
import difflib
import json
import os
import re
import shutil
import subprocess
import time
import wave

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:  # QQ 原生语音格式；装了 silk-python 才有
    from astrbot.core.utils.tencent_record_helper import wav_to_tencent_silk
except Exception:  # noqa: BLE001
    wav_to_tencent_silk = None

PLUGIN_NAME = "astrbot_plugin_dafeiyu_voice_tool"

# 整段语音模式的总开关：只有控制台（.sys/gui）会写它。
# 聊天里不再有任何自动切换路径（没有 llm 工具、没有关键词兜底）。
PANEL_KEY = "_panel"

DEFAULTS = {
    "max_seconds": 58,
    "max_chars": 240,
    "max_messages": 5,
    "chunk_gap_ms": 800,
    "mode_hours": 0,
    "voice_mode_default": False,
    "use_silk": True,
}
MAX_CHARS = 60  # 兼容旧行为；真正生效的是配置里的 max_chars
FLAG = "_dafeiyu_voice_spoken"
BREAKS = "，。！？、,.!?;；~～\n"
MD_STRIP = re.compile(r"[*_`#>~]+|\[(.*?)\]\(.*?\)")

# Telegram 语音条（OGG/Opus）切段上限；比 QQ 的 58 秒保守一点
TG_MAX_SECONDS = 55.0

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))


def find_ffmpeg() -> str:
    """优先 PATH 里的 ffmpeg（start-astrbot.cmd 会加），否则用项目自带的。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.path.join(_ROOT, "ffmpeg", "bin", "ffmpeg.exe")
    return local if os.path.isfile(local) else ""


async def wav_to_ogg(path: str) -> str:
    """WAV → Telegram 原生语音条格式 OGG/Opus；失败就退回原文件。"""
    ff = find_ffmpeg()
    if not ff or not path.lower().endswith(".wav"):
        return path
    out = os.path.splitext(path)[0] + ".tg.ogg"
    try:
        proc = await asyncio.create_subprocess_exec(
            ff, "-hide_banner", "-loglevel", "error", "-i", path,
            "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
            "-vbr", "on", "-y", out,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=NO_WINDOW,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 500:
            logger.info(
                f"[dafeiyu-voice] 转 Telegram OGG：{os.path.getsize(path)} → {os.path.getsize(out)} 字节"
            )
            return out
        logger.warning(f"[dafeiyu-voice] 转 OGG 失败（{proc.returncode}）：{(err or b'')[-160:]!r}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[dafeiyu-voice] 转 OGG 异常，直接发原文件: {e}")
    return path


def clip(text: str, limit: int = MAX_CHARS) -> str:
    """超长就在标点处断开，别把话截成半句。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(ch) for ch in BREAKS)
    return head[: cut + 1].rstrip() if cut >= limit // 3 else head.rstrip()


def clean_speech(text: str) -> str:
    """把要念的东西洗成纯口语：去 Markdown、去括号动作、压空白。"""
    s = MD_STRIP.sub("", str(text or ""))
    s = re.sub(r"[（(][^）)]{0,30}[）)]", "", s)
    return " ".join(s.split())


def split_text(text: str, limit: int) -> list[str]:
    """按标点切成若干段，每段尽量不超过 limit 字。"""
    text = clean_speech(text)
    if not text:
        return []
    limit = max(40, int(limit))
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        head = rest[:limit]
        cut = max(head.rfind(ch) for ch in BREAKS)
        if cut < limit // 2:  # 后半句没有标点，硬切
            cut = limit - 1
        parts.append(rest[: cut + 1].strip())
        rest = rest[cut + 1 :].strip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


def wav_seconds(path: str) -> float:
    """读 WAV 头算时长；头坏了就按数据量估（24k 单声道 16bit）。"""
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate() or 24000
            frames = w.getnframes()
            if 0 < frames < (rate or 24000) * 3600:
                return frames / float(rate)
            return (os.path.getsize(path) - 44) / float(
                rate * w.getnchannels() * w.getsampwidth()
            )
    except Exception:  # noqa: BLE001
        return max(0.0, (os.path.getsize(path) - 44) / 48000.0)


TAIL_SILENCE_SECONDS = 1.0


def ensure_tail_silence(path: str, seconds: float = TAIL_SILENCE_SECONDS) -> str:
    """在 WAV 末尾补一秒静音。

    Breeze 合成出来的最后半个字其实在，但 QQ 播放/SILK 解码会在尾巴上提前收口，
    听起来就像最后一个字没咬完。末尾垫一秒，让最后一个字的尾音完整落地。
    已经有一秒静音的文件直接复用，避免合成链路上重复补。
    """
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
        logger.warning(f"[dafeiyu-voice] 补尾部静音失败，继续用原文件: {e}")
        return path


class DafeiyuVoiceTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self._state_path = os.path.join(
            get_astrbot_plugin_data_path(), PLUGIN_NAME, "voice_mode.json"
        )

    # ---------- 配置 / 状态 ----------

    def cfg(self, key: str):
        default = DEFAULTS.get(key)
        try:
            if self.config is None:
                return default
            val = self.config.get(key, default)
            return default if val is None else val
        except Exception:  # noqa: BLE001
            return default

    def _load_state(self) -> dict:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._state_path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dafeiyu-voice] 保存语音模式失败: {e}")

    def mode_on(self, umo: str) -> bool:
        data = self._load_state()
        # 面板总开关优先：控制台点了「整段语音模式」，所有会话都按它来。
        panel = data.get(PANEL_KEY) or {}
        if panel.get("on"):
            hours = float(self.cfg("mode_hours") or 0)
            if hours > 0 and time.time() - float(panel.get("ts") or 0) > hours * 3600:
                logger.info(f"[dafeiyu-voice] 面板语音模式超时（{hours:g} 小时）自动关闭")
                return bool(self.cfg("voice_mode_default"))
            return True
        # 兼容旧版本留下的按会话记录（新版不会再写这种记录）。
        rec = data.get(umo) or {}
        if not rec.get("on"):
            return bool(self.cfg("voice_mode_default"))
        hours = float(self.cfg("mode_hours") or 0)
        if hours > 0 and time.time() - float(rec.get("ts") or 0) > hours * 3600:
            logger.info(f"[dafeiyu-voice] {umo} 的语音模式超时（{hours:g} 小时）自动关闭")
            return bool(self.cfg("voice_mode_default"))
        return True

    def set_mode(self, umo: str, on: bool) -> None:
        data = self._load_state()
        data[umo] = {"on": bool(on), "ts": time.time()}
        self._save_state(data)

    # ---------- 提供商 ----------

    def _fallback_provider(self, umo: str = ""):
        """AstrBot 的规矩是「语音总开关关掉就一律不给提供商」（provider/manager.py:291）。
        可整条语音的模式偏偏要求总开关关掉，两边打架，send_voice 就永远拿不到人。
        这里绕过开关：按配置里点名的 provider_id 直接去实例表里取，取不到再拿第一个能用的 TTS。"""
        try:
            pm = getattr(self.context, "provider_manager", None)
            if not pm:
                return None
            try:
                conf = self.context.get_config(umo) or {}
                pid = ((conf.get("provider_tts_settings") or {}).get("provider_id") or "").strip()
            except Exception:  # noqa: BLE001
                pid = ""
            inst_map = getattr(pm, "inst_map", {}) or {}
            if pid and pid in inst_map:
                logger.info(f"[dafeiyu-voice] 总开关关着，直接取提供商 {pid}")
                return inst_map[pid]
            for inst in getattr(pm, "tts_provider_insts", None) or []:
                if inst:
                    logger.info(
                        "[dafeiyu-voice] 用第一个可用的语音提供商 "
                        f"{getattr(inst, 'provider_config', {}).get('id')}"
                    )
                    return inst
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dafeiyu-voice] 兜底取提供商失败: {e}")
        return None

    async def get_provider(self, umo: str):
        provider = None
        try:
            provider = await self.context.get_using_tts_provider_async(umo)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dafeiyu-voice] 取语音提供商失败: {e}")
        if not provider:
            provider = self._fallback_provider(umo)
        return provider

    # ---------- 合成 / 发送 ----------

    async def synth_fit(self, provider, text: str, depth: int = 0, limit: float | None = None) -> list[str]:
        """合成一段；真实时长超过上限就对半切开重来。返回若干个音频文件路径。"""
        limit = float(limit or self.cfg("max_seconds") or 58)
        try:
            path = await provider.get_audio(text)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dafeiyu-voice] 合成失败（{len(text)} 字）: {e}")
            return []
        if not path or not os.path.exists(path):
            logger.error(f"[dafeiyu-voice] 没有拿到音频文件: {path!r}")
            return []
        secs = wav_seconds(path)
        if secs <= limit + 0.5 or depth >= 3 or len(text) <= 40:
            return [path]
        logger.info(f"[dafeiyu-voice] 这段合成出来 {secs:.1f}s 超过 {limit:.0f}s，切成两段重来")
        mid = len(text) // 2
        cut = max(text.rfind(ch, 0, mid + 20) for ch in BREAKS)
        if cut <= 0:
            cut = mid
        out: list[str] = []
        for piece in (text[: cut + 1], text[cut + 1 :]):
            piece = piece.strip()
            if piece:
                out.extend(await self.synth_fit(provider, piece, depth + 1, limit))
        return out

    async def to_send_file(self, path: str, platform: str = "") -> str:
        """按平台转成语音条：QQ→SILK，Telegram→OGG/Opus；不行就用原文件。"""
        # 先补尾部静音，再转格式；这样最后半个字不会被播放器提前收口
        path = await asyncio.to_thread(ensure_tail_silence, path)
        if platform == "telegram":
            return await wav_to_ogg(path)
        if not self.cfg("use_silk") or wav_to_tencent_silk is None:
            return path
        if not path.lower().endswith(".wav"):
            return path
        out = os.path.splitext(path)[0] + ".silk"
        try:
            await wav_to_tencent_silk(path, out)
            if os.path.exists(out) and os.path.getsize(out) > 100:
                logger.info(
                    f"[dafeiyu-voice] 转 silk：{os.path.getsize(path)} → {os.path.getsize(out)} 字节"
                )
                return out
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu-voice] 转 silk 失败，直接发原文件: {e}")
        return path

    async def speak(self, event: AstrMessageEvent, text: str, reason: str = "tool") -> int:
        """把一段话变成语音条发出去；超长自动切多条、逐条连着发。返回发了几条。"""
        umo = event.unified_msg_origin
        try:
            platform = event.get_platform_name() or ""
        except Exception:  # noqa: BLE001
            platform = ""
        provider = await self.get_provider(umo)
        if not provider:
            logger.error("[dafeiyu-voice] 没有可用的语音提供商")
            return 0

        limit_chars = int(self.cfg("max_chars") or 240)
        max_msgs = max(1, int(self.cfg("max_messages") or 5))
        pieces = split_text(text, limit_chars)[:max_msgs]
        if not pieces:
            return 0

        gap = max(0.0, float(self.cfg("chunk_gap_ms") or 800) / 1000.0)

        # Breeze 服务端一次只能跑一个请求，并发发过去会 409，反而卡死/超时；
        # 这里串行合成，配合 agent_runner.tool_call_timeout=600。
        results = []
        for piece in pieces:
            try:
                results.append(
                    await self.synth_fit(
                        provider, piece,
                        limit=TG_MAX_SECONDS if platform == "telegram" else None,
                    )
                )
            except Exception as e:  # noqa: BLE001
                results.append(e)
        prepared: list[tuple[str, list[str]]] = []
        for piece, files in zip(pieces, results):
            if isinstance(files, Exception) or not files:
                logger.error(f"[dafeiyu-voice] 这一段没合成出来，跳过：{piece[:20]}")
                continue
            prepared.append((piece, list(files)))

        sent = 0
        spoken_texts: list[str] = []
        for i, (piece, files) in enumerate(prepared):
            for path in files:
                send_path = await self.to_send_file(path, platform)
                try:
                    await event.send(MessageChain([Record(file=send_path)]))
                    sent += 1
                    spoken_texts.append(piece)
                    logger.info(
                        f"[dafeiyu-voice] 已发语音（{len(piece)} 字, "
                        f"{wav_seconds(path):.1f}s, {reason}）: {piece[:40]}"
                    )
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[dafeiyu-voice] 发送失败: {e}")
            if i < len(prepared) - 1 and gap:
                await asyncio.sleep(gap)
        if spoken_texts:
            event.set_extra(FLAG, "".join(spoken_texts))
        return sent

    # ---------- 工具 ----------

    @filter.llm_tool(name="send_voice")
    async def send_voice(self, event: AstrMessageEvent, text: str) -> str:
        """把你现在想对她说的话，用你自己的声音念出来，发成语音条（不是打字）。

        什么时候用：撒娇、哄人、道晚安、被逗笑了、想用语气表达情绪；或者用户已经让你
        用语音说话时，把你这一段回复念出来。
        什么时候别用：写代码、列步骤、表格、链接、需要她复制粘贴的内容——这些直接打字。
        念出来的必须是纯口语：不要括号动作、表情符号、列表、Markdown、颜文字。
        一次可以念到大约一分钟（约 240 字）；更长的会自动拆成第一条、第二条……连着发。

        Args:
            text(string): 要念出来的话，纯口语，最长约 240 字（超了自动分段）
        """
        spoken = clean_speech(text)
        if not spoken:
            return "（没有内容，没发语音。请直接用文字回复）"
        sent = await self.speak(event, spoken, reason="tool")
        if not sent:
            return "（语音合成失败，请直接用文字回复这句话）"
        return f"（已用语音说完了，共 {sent} 条。不要再打字重复这句话，也不用补充，直接结束这轮）"

    # 整段语音模式只由控制台的手动开关控制：
    # 这里刻意**不提供** llm 工具，也没有关键词兜底 —— 她在聊天里没有开关权。

    # ---------- 结果拦截 ----------

    @filter.on_decorating_result()
    async def shape_result(self, event: AstrMessageEvent):
        """两个职责：
        1. 这轮已经用 send_voice 发过语音 → 只剩重复文字就丢掉，免得同一句既语音又打字。
        2. 整段语音模式开着 → 把这条文字回复念成语音条（长内容切成几条连着发），文字不再发。
        """
        spoken = event.get_extra(FLAG)
        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return
        try:
            if not result.is_llm_result():
                return
        except Exception:  # noqa: BLE001
            pass

        umo = event.unified_msg_origin

        # 1) 已经发过语音：丢掉重复文字
        if spoken:
            event.set_extra(FLAG, None)
            left = "".join(
                comp.text
                for comp in result.chain
                if isinstance(comp, Plain) and getattr(comp, "text", None)
            ).strip()
            if not left:
                return
            ratio = difflib.SequenceMatcher(None, left, spoken).ratio()
            if len(left) <= 20 or ratio >= 0.45 or left in spoken:
                logger.info(f"[dafeiyu-voice] 丢掉重复文字（相似度 {ratio:.2f}）: {left}")
                result.chain.clear()
            return

        # 2) 整段语音模式：文字 → 语音（这一轮已经有语音条就不再重复）
        if not self.mode_on(umo):
            return
        if any(isinstance(c, Record) for c in result.chain):
            return
        text = "".join(
            comp.text
            for comp in result.chain
            if isinstance(comp, Plain) and getattr(comp, "text", None)
        ).strip()
        if not text or len(text) > 4000:  # 极端长文别全念
            return
        sent = await self.speak(event, text, reason="mode")
        if sent:
            logger.info(f"[dafeiyu-voice] 整段语音模式：把 {len(text)} 字念成 {sent} 条语音")
            result.chain = [c for c in result.chain if not isinstance(c, Plain)]