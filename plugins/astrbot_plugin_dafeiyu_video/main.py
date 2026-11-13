"""大肥鱼看视频：DeepSeek 只能看图、看不了视频，AstrBot 收到视频也只往对话里塞一行文件名。
这里在发给模型之前（on_llm_request）把视频均匀截几张画面，作为图片交给模型；再把声音转成文字：
有本机语音识别（install-asr.ps1 装的 sherpa-onnx + SenseVoice，加 -Plus 多装方言、歌词更准的 Fun-ASR-Nano）就本机认，免费；没有就用小米 MiMo（mimo-v2.5-asr，key 借 AstrBot 里 xiaomi 服务商的，11-03 实测 0.6 秒认对）；再没有才用硅基流动的 key；都没有只看画面。

- 截的画面默认只给这一轮用（mark_as_temp），不进聊天记录，后面每轮不会重复按图收钱；
  文字说明（含转写）也只给这一轮用，不再反复写进上下文。
  代价是追问细节她答不上（10-27 实测：追问「第一个画面有人吗」，她编了个「没人」），所以说明里让她记不清就直说。
  想让她记住画面就打开 keep_frames，之后每轮都带着这几张图（DeepSeek 前缀缓存命中按低价算，但还是多花钱）。
- 电脑 QQ 发的视频常常是「文件」（扩展名 mp4、mov 这些），也当视频看（10-29 加）。
- 私聊都看；群里默认被 @ 或叫醒才看（群聊插件 group_chat_plus 也是走 event.request_llm，这个钩子同样生效）。
- QQ 语音（10-28 加）：私聊、群里发来的语音，用同一套识别转成「[语音：……]」一行字，排在群聊插件和对话模型前面，
  她就当对方打了这句字。QQ 语音多半是 silk 格式，ffmpeg 不认，先用 AstrBot 自带的转换工具转成 wav。
- 出任何错都只记日志，不影响她正常回话。
"""

import os
import shutil
import time
import uuid

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain, Record, Reply, Video
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import ImageURLPart, TextPart
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from . import video as V


# 电脑 QQ 拖进去、粘贴进去的视频是按「文件」发的（10-29 Mac QQ 实测，「图片」按钮也不收 mp4），扩展名是这些的文件也当视频看
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".3gp", ".wmv", ".ts")


def _is_video(comp) -> bool:
    if isinstance(comp, Video):
        return True
    return isinstance(comp, File) and (comp.name or "").lower().endswith(VIDEO_EXTS)


def _first_video(event: AstrMessageEvent):
    for comp in event.message_obj.message:
        if _is_video(comp):
            return comp
    for comp in event.message_obj.message:
        if isinstance(comp, Reply) and comp.chain:
            for c in comp.chain:
                if _is_video(c):
                    return c
    return None


def _is_astrbot_marker(part, name: str = "") -> bool:
    text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
    if not isinstance(text, str):
        return False
    return text.startswith("[Video Attachment") or bool(name) and text.startswith(f"[File Attachment: name {name},")


class DafeiyuVideo(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.root = os.path.join(get_astrbot_temp_path(), "dafeiyu_video")
        self.warned_no_ffmpeg = False

    def _purge_old(self):
        """上一轮的截图交给模型后就没用了，超过一小时的目录清掉。"""
        if not os.path.isdir(self.root):
            return
        for name in os.listdir(self.root):
            d = os.path.join(self.root, name)
            try:
                if os.path.isdir(d) and time.time() - os.path.getmtime(d) > 3600:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    # 群聊插件处理群消息的 handler 是 priority=-1，这里要排在它前面
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def hear(self, event: AstrMessageEvent):
        if not self.conf.get("listen_voice", True):
            return
        chain = event.message_obj.message
        rec = next((c for c in chain if isinstance(c, Record)), None)
        if not rec:
            return
        t0 = time.time()
        try:
            text, how = await self._hear(rec)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu_video] 这条语音没听成：{type(e).__name__}: {e}")
            return
        chain[chain.index(rec)] = Plain(f"[语音：{text}]" if text else "[发来一条语音，没听出说了什么]")
        s = "".join(c.text for c in chain if isinstance(c, Plain)).strip()
        event.message_obj.message_str = s
        event.message_str = s
        logger.info(f"[dafeiyu_video] 听了一条语音（{how}）：{len(text)} 字，用时 {time.time() - t0:.1f} 秒")

    def _mimo_key(self) -> tuple[str, str]:
        """小米 MiMo 的 key 直接借 AstrBot 里 xiaomi 那家服务商的，不另存一份密钥。"""
        try:
            cfg = self.context.get_config() or {}
            for s in (cfg.get("provider_sources") or []):
                if s.get("id") == "xiaomi":
                    k = s.get("key")
                    if isinstance(k, list):
                        k = k[0] if k else ""
                    base = (s.get("api_base") or "").strip() or V.MIMO_API_BASE
                    return (k or "").strip(), base
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dafeiyu_video] 读小米 key 没成功：{e}")
        return "", V.MIMO_API_BASE

    def _mimo_ready(self) -> tuple[bool, str, str]:
        key, base = self._mimo_key()
        on = bool(self.conf.get("mimo_asr", True)) and bool(key)
        return on, key, base

    def _use_mimo_first(self, asr: str, mimo_on: bool) -> bool:
        """mimo_first 打开、或者本机识别没装，就先问小米；否则本机识别优先（免费）。"""
        return bool(mimo_on) and (bool(self.conf.get("mimo_first", False)) or not asr)

    async def _hear(self, rec):
        ffmpeg = V.find_ffmpeg((self.conf.get("ffmpeg_path") or "").strip())
        asr = V.find_asr((self.conf.get("asr_dir") or "").strip(), ffmpeg)
        key = (self.conf.get("asr_key") or "").strip()
        mimo_on, mimo_key, mimo_base = self._mimo_ready()
        if not ffmpeg or not (asr or mimo_on or key):
            raise RuntimeError("没装 ffmpeg 或语音识别")
        path = await rec.convert_to_file_path()
        self._purge_old()
        work = os.path.join(self.root, uuid.uuid4().hex[:12])
        os.makedirs(work, exist_ok=True)
        with open(path, "rb") as f:
            head = f.read(12)
        if b"#!SILK" in head:
            from astrbot.core.utils.tencent_record_helper import tencent_silk_to_wav
            path = await tencent_silk_to_wav(path, os.path.join(work, "voice.wav"))
        # 谁先谁后：mimo_first 打开（或本机识别没装）就先小米，否则本机识别优先（免费）
        order = (["local"] if asr else []) + (["mimo"] if mimo_on else []) + ["siliconflow"]
        if self._use_mimo_first(asr, mimo_on) and "mimo" in order:
            order.remove("mimo")
            order.insert(0, "mimo")
        last = None
        for which in order:
            try:
                if which == "local" and asr:
                    chunks = await V.grab_chunks(ffmpeg, path, 120, work)
                    if not chunks:
                        return "", "本机识别"
                    text, how = await V.transcribe_local(asr, chunks)
                    return text, f"本机识别（{how}）"
                if which == "mimo" and mimo_on:
                    chunks = await V.grab_chunks(ffmpeg, path, 120, work)
                    if not chunks:
                        return "", "小米 MiMo"
                    model = (self.conf.get("mimo_asr_model") or "").strip() or V.MIMO_ASR_MODEL
                    return await V.transcribe_mimo_chunks(chunks, mimo_key, model, mimo_base), "小米 MiMo"
                if which == "siliconflow" and key:
                    audio = await V.grab_audio(ffmpeg, path, 120, work)
                    model = self.conf.get("asr_model") or "FunAudioLLM/SenseVoiceSmall"
                    return (await V.transcribe(audio, key, model) if audio else ""), "硅基流动"
            except Exception as e:  # noqa: BLE001
                last = e
                logger.warning(f"[dafeiyu_video] 语音识别（{which}）没成，换下一个：{type(e).__name__}: {e}")
        raise RuntimeError(f"语音识别都没成：{last}")

    @filter.on_llm_request()
    async def look(self, event: AstrMessageEvent, req):
        video = _first_video(event)
        if not video:
            return
        if not event.is_private_chat() and self.conf.get("group_need_at", True) and not event.is_at_or_wake_command:
            return
        try:
            await self._look(event, req, video)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu_video] 这段视频没看成：{type(e).__name__}: {e}")

    async def _look(self, event, req, video):
        ffmpeg = V.find_ffmpeg((self.conf.get("ffmpeg_path") or "").strip())
        if not ffmpeg:
            if not self.warned_no_ffmpeg:
                logger.warning("[dafeiyu_video] 没找到 ffmpeg，看不了视频。装好 ffmpeg 后在插件设置里填 ffmpeg.exe 的位置")
                self.warned_no_ffmpeg = True
            req.extra_user_content_parts.append(TextPart(
                text="[对方发来一段视频，但你这边看不了视频内容（电脑上没装 ffmpeg）。老实说没看到，别猜内容。]").mark_as_temp())
            return

        t0 = time.time()
        path = await video.get_file() if isinstance(video, File) else await video.convert_to_file_path()
        max_seconds = int(self.conf.get("max_seconds", 180) or 180)
        max_frames = max(1, int(self.conf.get("max_frames", 6) or 6))
        duration, has_audio = await V.probe(ffmpeg, path)
        times = V.pick_times(duration, max_seconds, max_frames)

        self._purge_old()
        work = os.path.join(self.root, uuid.uuid4().hex[:12])
        frames = await V.grab_frames(ffmpeg, path, times, work)
        if not frames:
            logger.warning("[dafeiyu_video] ffmpeg 没截出画面（视频格式不认识？）")
            return

        heard, how = "", ""
        asr = V.find_asr((self.conf.get("asr_dir") or "").strip(), ffmpeg)
        key = (self.conf.get("asr_key") or "").strip()
        mimo_on, mimo_key, mimo_base = self._mimo_ready()
        if has_audio and (asr or mimo_on or key):
            order = (["local"] if asr else []) + (["mimo"] if mimo_on else []) + ["siliconflow"]
            if self._use_mimo_first(asr, mimo_on) and "mimo" in order:
                order.remove("mimo")
                order.insert(0, "mimo")
            for name in order:
                try:
                    if name == "local":
                        chunks = await V.grab_chunks(ffmpeg, path, max_seconds, work)
                        if not chunks:
                            continue
                        heard, which = await V.transcribe_local(asr, chunks)
                        how = f"本机识别（{which}）"
                    elif name == "mimo":
                        chunks = await V.grab_chunks(ffmpeg, path, max_seconds, work)
                        if not chunks:
                            continue
                        model = (self.conf.get("mimo_asr_model") or "").strip() or V.MIMO_ASR_MODEL
                        heard = await V.transcribe_mimo_chunks(chunks, mimo_key, model, mimo_base)
                        how = "小米 MiMo"
                    else:
                        if not key:
                            continue
                        audio = await V.grab_audio(ffmpeg, path, max_seconds, work)
                        if not audio:
                            continue
                        heard = await V.transcribe(audio, key, self.conf.get("asr_model") or "FunAudioLLM/SenseVoiceSmall")
                        how = "硅基流动"
                    if heard:
                        break
                except Exception as e:  # noqa: BLE001
                    how = name
                    logger.warning(f"[dafeiyu_video] 声音转文字没成功（{name}），换下一个：{e}")

        keep = bool(self.conf.get("keep_frames", False))
        length = V.mmss(duration) if duration else "未知"
        cut = f"，你只看了前 {max_seconds // 60} 分钟" if duration > max_seconds else ""
        stamps = "、".join(V.mmss(t) for t, _ in frames)
        if heard:
            sound = f"视频里说的话（自动转写，可能有错字）：「{heard[:1500]}」。"
        elif has_audio and how:
            sound = "视频有声音，但没听出有人说话（可能只有音乐或杂音）。"
        elif has_audio:
            sound = "视频有声音，但你这次只看到了画面，没听到声音。"
        else:
            sound = ""
        text = (f"[对方发来一段视频，时长 {length}{cut}。下面 {len(frames)} 张图是从视频里按时间顺序截的画面"
                f"（分别在 {stamps}）。{sound}回的时候就当你刚看完这段视频，别提截图、抽帧、转写这些词。"
                f"{'' if keep else '这些画面只给你看这一次，之后被问到你这次没说到的细节，就说当时没看清，别编。'}]")

        fname = video.name if isinstance(video, File) else ""
        req.extra_user_content_parts = [p for p in req.extra_user_content_parts if not _is_astrbot_marker(p, fname)]
        req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        for _, f in frames:
            part = ImageURLPart(image_url=ImageURLPart.ImageURL(url=f))
            req.extra_user_content_parts.append(part if keep else part.mark_as_temp())
        logger.info(f"[dafeiyu_video] 看了一段 {length} 的视频：{len(frames)} 张画面，"
                    f"{how + '转写 ' + str(len(heard)) + ' 字' if heard else (how + '没认出字' if how else '没转写')}，用时 {time.time() - t0:.1f} 秒")
