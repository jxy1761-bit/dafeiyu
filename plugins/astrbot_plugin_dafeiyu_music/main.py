"""大肥鱼听歌

有人发歌 / 带音乐的 MV 时：
1. ffmpeg 抽出音轨；
2. 本机 MOSS-Audio-4B 真听一遍（风格、情绪、配器、结构、歌词）；
3. Demucs 分出人声，留着给她翻唱；
4. 她说「唱两句」时，用 Seed-VC 把副歌换成 Breeze 音色，发语音条。

所有模型和输出都在工作区 .sys/audio-lab，不占别的盘。
"""

import asyncio
import json
import os
import re
import shutil
import time
import urllib.request
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Music, Plain, Record, Reply, Video
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".3gp", ".wmv", ".ts")
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".amr", ".silk")

PROMPT = (
    "请用中文简洁回答（总共不超过 300 字）：1) 风格和情绪；2) 主要乐器；3) 人声特点；"
    "4) 副歌大概从第几秒开始；5) 副歌歌词（能听清就写两句）。"
    "如果只是说话没有音乐，直接说明这是语音，不要硬编。"
)

NO_WINDOW = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)


def _workspace() -> str:
    # plugin dir = <workspace>/astrbot/data/plugins/<plugin>
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def _file_hint(comp) -> str:
    return str(getattr(comp, "name", "") or getattr(comp, "file_", "") or "").lower()


def _is_video(comp) -> bool:
    if isinstance(comp, Video):
        return True
    return isinstance(comp, File) and _file_hint(comp).endswith(VIDEO_EXTS)


def _is_audio(comp) -> bool:
    return isinstance(comp, File) and _file_hint(comp).endswith(AUDIO_EXTS)


MUSIC_HINTS = ("歌", "唱", "mv", "MV", "音乐", "曲子", "旋律", "听歌", "伴奏", "bgm", "BGM",
               "翻唱", "副歌", "这首", "来一段", "现场")


def _pick_media(event, allow_record: bool = False):
    chain = event.message_obj.message
    for comp in chain:
        if _is_video(comp) or _is_audio(comp):
            return comp
    for comp in chain:  # 引用一条之前发的歌/视频再说「唱两句」
        if isinstance(comp, Reply) and comp.chain:
            for c in comp.chain:
                if _is_video(c) or _is_audio(c):
                    return c
    if allow_record:  # 只有 listen_song 工具会走这里，普通语音不自动听
        for comp in chain:
            if isinstance(comp, Record):
                return comp
    return None


def _looks_like_music(event, comp) -> bool:
    """音频文件直接听；视频要她本来就提到歌/MV 才听，免得每个视频都等两分钟。"""
    if _is_audio(comp):
        return True
    text = (getattr(event, "message_str", "") or "").lower()
    return any(h.lower() in text for h in MUSIC_HINTS)


class DafeiyuMusic(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.conf = config
        self.ws = _workspace()
        self.lab = (self._cfg("audio_lab_dir") or "").strip() or os.path.join(self.ws, ".sys", "audio-lab")
        self.jobs = os.path.join(self.lab, "out", "jobs")
        self.ffmpeg = (self._cfg("ffmpeg_path") or "").strip() or os.path.join(self.ws, "ffmpeg", "bin", "ffmpeg.exe")
        self.py_svc = os.path.join(self.lab, "venv", "Scripts", "python.exe")
        self.py_moss = os.path.join(self.lab, "venv-moss", "Scripts", "python.exe")
        self.moss_model = os.path.join(self.lab, "repos", "MOSS-Audio-4B-Instruct")
        self.seedvc = os.path.join(self.lab, "repos", "seed-vc")
        self.ref = (self._cfg("ref_wav") or "").strip() or os.path.join(
            self.ws, "breeze", "voices", "dafeiyu_mihu_seedvc.wav")
        self.cache: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self._restore_task: asyncio.Task | None = None

    def _cfg(self, key):
        try:
            return self.conf.get(key) if self.conf is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _music_enabled(self) -> bool:
        """控制台里的独立开关；文件不存在时默认关闭，一键启动不会碰它。"""
        try:
            with open(os.path.join(self.lab, "music_enabled.json"), "r", encoding="utf-8") as f:
                return bool(json.load(f).get("enabled"))
        except Exception:  # noqa: BLE001
            return False

    # ---------- 模型交接 ----------

    def _console_base(self) -> str:
        try:
            p = os.path.join(self.ws, ".sys", "gui", "state", "console.port")
            with open(p, "r", encoding="utf-8") as f:
                port = int(f.read().strip() or 7788)
        except Exception:  # noqa: BLE001
            port = 7788
        return f"http://127.0.0.1:{port}"

    def _console_post_sync(self, path: str, timeout: float = 90) -> dict:
        req = urllib.request.Request(
            self._console_base() + path, data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    async def _console_post(self, path: str, timeout: float = 90) -> dict:
        return await asyncio.to_thread(self._console_post_sync, path, timeout)

    async def _free_for_music(self) -> None:
        if self._restore_task and not self._restore_task.done():
            self._restore_task.cancel()
        self._restore_task = None
        try:
            r = await self._console_post("/api/model/free_for_music", timeout=120)
            logger.info(f"[dafeiyu-music] 腾显存：{r.get('message') or r}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu-music] 腾显存失败（继续试）：{type(e).__name__}: {e}")

    async def _restore_voice(self) -> None:
        if self._restore_task and not self._restore_task.done():
            self._restore_task.cancel()
        self._restore_task = None
        try:
            r = await self._console_post("/api/model/restore_voice", timeout=30)
            logger.info(f"[dafeiyu-music] 恢复语音：{r.get('message') or r}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu-music] 恢复语音失败：{type(e).__name__}: {e}")

    async def _delayed_restore(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._restore_voice()

    def _schedule_restore(self, delay: float = 180) -> None:
        if self._restore_task and not self._restore_task.done():
            self._restore_task.cancel()
        self._restore_task = asyncio.create_task(self._delayed_restore(delay))

    async def _ensure_f16_ref(self) -> None:
        """没有 F16 翻唱参考时，让控制台临时起 F16 生成一份再卸掉。"""
        if self._cfg("sing_f16_ref") is False:
            return
        try:
            ref = self._current_ref()
            if "seedvc_f16" in ref:
                return
            r = await self._console_post("/api/model/voice_f16ref", timeout=300)
            logger.info(f"[dafeiyu-music] F16 参考：{r.get('message') or r.get('error')}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu-music] F16 参考没生成，退回普通参考：{type(e).__name__}: {e}")

    def _f16_active(self) -> bool:
        """F16 和 MOSS/Seed-VC 抢显存；F16 在跑时听歌基本跑不动。"""
        try:
            p = os.path.join(self.ws, ".sys", "gui", "state", "voice_engine.json")
            with open(p, "r", encoding="utf-8") as f:
                return str(json.load(f).get("engine") or "").lower() == "f16"
        except Exception:  # noqa: BLE001
            return False

    # ---------- 外部进程 ----------

    def _env(self) -> dict:
        env = os.environ.copy()
        env.update({
            "HIP_VISIBLE_DEVICES": "1",
            "ROCR_VISIBLE_DEVICES": "1",
            "CUDA_VISIBLE_DEVICES": "1",
            "HF_ENDPOINT": "https://hf-mirror.com",
            "HF_HOME": os.path.join(self.lab, "cache", "hf"),
            "TORCH_HOME": os.path.join(self.lab, "cache", "torch"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        })
        return env

    async def _run(self, args: list[str], cwd: str | None = None, timeout: int = 900) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
            creationflags=NO_WINDOW,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"超时（{timeout}s）：{os.path.basename(args[0])}")
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def _extract(self, media_path: str, out: str, max_seconds: int) -> str:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        code, _, err = await self._run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", media_path,
            "-t", str(max_seconds), "-vn", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", "-y", out,
        ], timeout=180)
        if code != 0 or not os.path.isfile(out) or os.path.getsize(out) < 1000:
            raise RuntimeError(f"ffmpeg 抽音轨失败：{err[-300:]}")
        return out

    async def _probe(self, path: str) -> float:
        _, _, err = await self._run([self.ffmpeg, "-hide_banner", "-i", path], timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
        if not m:
            return 0.0
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    async def _moss(self, mix: str, max_seconds: int) -> str:
        code, out, err = await self._run([
            self.py_moss, os.path.join(self.lab, "run_moss.py"),
            "--model", self.moss_model, "--audio", mix,
            "--prompt", PROMPT, "--max-seconds", str(max_seconds), "--max-new-tokens", "360",
        ], cwd=self.lab, timeout=900)
        text = out.strip()
        if code != 0 or not text:
            raise RuntimeError(f"MOSS 没出声（退出码 {code}）：{err[-300:]}")
        return text

    async def _demucs(self, mix: str, out_dir: str) -> str:
        await self._run([
            self.py_svc, os.path.join(self.lab, "run_demucs.py"),
            "-n", "htdemucs", "--two-stems=vocals", "-o", out_dir, mix,
        ], cwd=self.lab, timeout=900)
        stem = os.path.splitext(os.path.basename(mix))[0]
        vocals = os.path.join(out_dir, "htdemucs", stem, "vocals.wav")
        return vocals if os.path.isfile(vocals) and os.path.getsize(vocals) > 1000 else ""

    def _current_ref(self) -> str:
        """换声插件切了音色就用它生成的 Seed-VC 参考，让翻唱和说话同一个音色。"""
        # AstrBot 进程和控制台对 plugin_data 的解析可能不是同一个根目录，两个都看
        paths = [
            os.path.join(self.ws, "astrbot", "data", "plugin_data",
                         "astrbot_plugin_dafeiyu_voice_switch", "voice_state.json"),
            os.path.join(get_astrbot_plugin_data_path(),
                         "astrbot_plugin_dafeiyu_voice_switch", "voice_state.json"),
        ]
        for p in paths:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if self._cfg("sing_f16_ref") is not False:
                    ref16 = str(data.get("ref_wav_f16") or "")
                    if ref16 and os.path.isfile(ref16):
                        return ref16
                ref = str(data.get("ref_wav") or "")
                if ref and os.path.isfile(ref):
                    return ref
            except Exception:  # noqa: BLE001
                continue
        return self.ref

    async def _send_singing(self, event: AstrMessageEvent, wav: str) -> int:
        """把唱好的整段切成不超过 QQ 上限的语音条，一条条发。"""
        dur = await self._probe(wav)
        piece = float(self._cfg("sing_piece_seconds") or 50)
        max_msgs = max(1, int(self._cfg("max_sing_messages") or 6))
        work = os.path.dirname(wav)
        starts = [0.0] if dur <= piece + 2 else [i * piece for i in range(int(dur / piece) + 1)]
        parts: list[str] = []
        for idx, st in enumerate(starts[:max_msgs]):
            part = os.path.join(work, f"sing_part_{idx:02d}.wav")
            if len(starts) == 1:
                cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
                       "-i", wav, "-af", "apad=pad_dur=1", "-ar", "44100", "-ac", "1",
                       "-y", part]
            else:
                cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
                       "-ss", f"{st:.2f}", "-t", f"{piece:.1f}", "-i", wav,
                       "-af", "apad=pad_dur=1", "-ar", "44100", "-ac", "1", "-y", part]
            code, _, err = await self._run(cmd, timeout=180)
            if code == 0 and os.path.isfile(part) and os.path.getsize(part) > 1000:
                parts.append(part)
            else:
                logger.warning(f"[dafeiyu-music] 切唱段失败：{err[-160:]}")
        try:
            platform = event.get_platform_name() or ""
        except Exception:  # noqa: BLE001
            platform = ""
        sent = 0
        for part in parts:
            send_path = part
            if platform == "telegram":
                # Telegram 语音条只认 OGG/Opus；转不了就发 wav（会变成文件，但仍能听）
                ogg = os.path.splitext(part)[0] + ".tg.ogg"
                code, _, err = await self._run([
                    self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", part,
                    "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
                    "-vbr", "on", "-y", ogg,
                ], timeout=120)
                if code == 0 and os.path.isfile(ogg) and os.path.getsize(ogg) > 500:
                    send_path = ogg
                else:
                    logger.warning(f"[dafeiyu-music] 唱段转 OGG 失败，直接发 wav：{err[-160:]}")
            else:
                try:
                    from astrbot.core.utils.tencent_record_helper import wav_to_tencent_silk
                    silk = os.path.splitext(part)[0] + ".silk"
                    await wav_to_tencent_silk(part, silk)
                    if os.path.exists(silk) and os.path.getsize(silk) > 100:
                        send_path = silk
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[dafeiyu-music] 唱段转 silk 失败，直接发 wav：{e}")
            try:
                await event.send(MessageChain([Record(file=send_path)]))
                sent += 1
                await asyncio.sleep(0.8)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[dafeiyu-music] 唱段发送失败：{e}")
        return sent

    async def _sing(self, vocals: str, start: float, seconds: float, semitones: int) -> str:
        vocals = os.path.abspath(vocals)
        work = os.path.dirname(vocals)
        target = self._current_ref()
        seg = os.path.join(work, "sing_src.wav")
        cut = min(max(0.0, start), max(0.0, await self._probe(vocals) - 1.0))
        code, _, err = await self._run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{cut:.2f}", "-t", f"{seconds:.1f}",
            "-i", vocals, "-ar", "44100", "-ac", "1", "-y", seg,
        ], timeout=120)
        if code != 0:
            raise RuntimeError(f"截副歌失败：{err[-200:]}")
        out_dir = os.path.join(work, f"sing_{uuid.uuid4().hex[:6]}")
        os.makedirs(out_dir, exist_ok=True)
        steps = int(self._cfg("seedvc_steps") or 30)
        if seconds > float(self._cfg("long_sing_threshold") or 25):
            steps = int(self._cfg("long_sing_steps") or 18)
        logger.info(f"[dafeiyu-music] Seed-VC 唱 {seconds:.0f}s，diffusion steps={steps}")
        code, out, err = await self._run([
            self.py_svc, "inference.py", "--source", seg, "--target", target, "--output", out_dir,
            "--f0-condition", "True", "--fp16", "True",
            "--diffusion-steps", str(steps),
            "--semi-tone-shift", str(int(semitones or 0)),
        ], cwd=self.seedvc, timeout=1800)
        wavs = [os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.lower().endswith(".wav")]
        if code != 0 or not wavs:
            raise RuntimeError(f"Seed-VC 没唱出来（退出码 {code}）：{err[-300:]}")
        return max(wavs, key=os.path.getmtime)

    # ---------- 主流程 ----------

    def _purge(self):
        if not os.path.isdir(self.jobs):
            return
        now = time.time()
        for name in os.listdir(self.jobs):
            d = os.path.join(self.jobs, name)
            try:
                if os.path.isdir(d) and now - os.path.getmtime(d) > 7200:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    async def _prepare(self, event: AstrMessageEvent, comp) -> dict:
        path = await comp.get_file() if isinstance(comp, File) else await comp.convert_to_file_path()
        self._purge()
        job = os.path.join(self.jobs, uuid.uuid4().hex[:12])
        os.makedirs(job, exist_ok=True)
        max_seconds = int(self._cfg("max_seconds") or 300)
        analyze_seconds = int(self._cfg("analyze_seconds") or 90)
        mix = await self._extract(path, os.path.join(job, "mix.wav"), max_seconds)
        logger.info(f"[dafeiyu-music] 抽好音轨（{max_seconds}s），开始听前 {analyze_seconds}s：{mix}")

        notes, audio_err = "", ""
        try:
            notes = await self._moss(mix, analyze_seconds)
        except Exception as e:  # noqa: BLE001
            audio_err = str(e)
            logger.warning(f"[dafeiyu-music] MOSS 没听成：{e}")

        vocals = ""
        try:
            vocals = await self._demucs(mix, os.path.join(job, "demucs"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu-music] 人声分离没成：{e}")

        info = {"ts": time.time(), "mix": mix, "vocals": vocals or mix, "notes": notes}
        self.cache[event.unified_msg_origin] = info
        return info

    @filter.on_llm_request()
    async def listen(self, event: AstrMessageEvent, req):
        if not self._music_enabled():
            return
        comp = _pick_media(event)
        if not comp:
            # 音乐分享卡片只有链接时没有可听的音频，直接告诉她情况
            try:
                chain = event.message_obj.message
                if any(isinstance(c, Music) for c in chain):
                    req.extra_user_content_parts.append(TextPart(
                        text="[对方发来一张音乐分享卡片；如果卡片里只有网页链接，你听不到音频，"
                             "请让对方发音频/视频文件或录屏。]").mark_as_temp())
                elif any(isinstance(c, File) or isinstance(c, Video) for c in chain):
                    req.extra_user_content_parts.append(TextPart(
                        text="[对方发来文件/视频；如果里面是歌/MV，调用 listen_song 听，"
                             "不要用 shell 搜索或转码文件。普通文件不用听。]").mark_as_temp())
            except Exception:  # noqa: BLE001
                pass
            return
        if not event.is_private_chat() and self._cfg("group_need_at") and not event.is_at_or_wake_command:
            return
        if self._cfg("video_need_hint") is not False and not _looks_like_music(event, comp):
            if _is_video(comp):
                req.extra_user_content_parts.append(TextPart(
                    text="[对方发来一段视频；如果你判断是歌/MV，调用 listen_song 认真听一遍再回答，"
                         "不要用 shell 搜索或转码文件。普通视频不用听。]").mark_as_temp())
            return
        try:
            await event.send(MessageChain([Plain("（戴上耳机，听一下…）")]))
        except Exception:  # noqa: BLE001
            pass
        await self._free_for_music()
        try:
            async with self.lock:
                info = await self._prepare(event, comp)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu-music] 这首歌没听成：{type(e).__name__}: {e}")
            await self._restore_voice()
            return

        notes = info.get("notes") or ""
        tip = (
            "[对方发来一段带音乐的视频/音频，你刚才戴上耳机认真听了一遍。"
            + (f"你听到的是：\n{notes[:1800]}\n" if notes else "但这台电脑的音乐模型没出声，你只听到了含糊的一片。\n")
            + "就当你真的听过这首歌：可以点评、吐槽、跟着哼、说哪句歌词戳你。"
              "别提模型、分析、转写这些词；没听清的地方就说没听清，别编。"
              "如果对方让你唱两句，调用 sing_song，把你听到的副歌用你自己的声音唱出来。"
              "不要用 shell 搜索或转码文件，直接用听到的内容回答。"
        )
        req.extra_user_content_parts.append(TextPart(text=tip).mark_as_temp())
        self._schedule_restore(180)
        logger.info(f"[dafeiyu-music] 听完了：{len(notes)} 字分析，人声={'有' if info['vocals'] != info['mix'] else '无'}")

    # ---------- 工具：主动听歌 ----------

    @filter.llm_tool(name="listen_song")
    async def listen_song(self, event: AstrMessageEvent) -> str:
        """认真听一下对方刚发来的歌 / 带音乐的 MV / 哼唱，然后按你听到的内容回答。

        什么时候用：对方发来歌、MV、音频或哼唱，想让你评价、识别或翻唱；
        系统没有自动听（比如视频里没提「歌/MV」）时，主动调用这个工具听一遍。
        普通说话的 QQ 语音不要用；对方只发了音乐分享卡片、没有音频文件时也不要硬听。
        不要用 shell 搜索/转码文件，直接调用这个工具。
        """
        if not self._music_enabled():
            return "（听歌/翻唱现在关着，要去大肥鱼控制台打开）"
        comp = _pick_media(event, allow_record=True)
        if not comp:
            return "（这条消息里没有可听的音频/视频；如果只是音乐分享卡片，让对方发文件或录屏）"
        if not event.is_private_chat() and self._cfg("group_need_at") and not event.is_at_or_wake_command:
            return "（群里要先 @ 她才能听）"
        try:
            await event.send(MessageChain([Plain("（戴上耳机，听一下…）")]))
        except Exception:  # noqa: BLE001
            pass
        await self._free_for_music()
        try:
            async with self.lock:
                info = await self._prepare(event, comp)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu-music] listen_song 没听成：{type(e).__name__}: {e}")
            await self._restore_voice()
            return "（这次没听成，别硬编；可以直接跟她说话）"
        self._schedule_restore(180)
        notes = (info.get("notes") or "").strip()
        if not notes:
            return "（没听清这首歌，别编；可以直接跟她说话）"
        return ("你听到的内容：\n" + notes[:1800] +
                "\n（如果你觉得合适可以点评；对方让你唱就调用 sing_song。）")

    # ---------- 工具：唱两句 ----------

    @filter.llm_tool(name="sing_song")
    async def sing_song(self, event: AstrMessageEvent, start: float = 0, seconds: float = 45,
                        semitones: float = 0, full: bool = False) -> str:
        """用你自己的声音，把刚才听过的歌翻唱成语音条（可以唱一分钟甚至整首）。

        什么时候用：对方让你「唱两句」「学一下」「用你的声音来一段」，或者你刚听完一首歌、想哼给她听。
        前提：必须已经听过（系统自动听过，或者你调用过 listen_song），缓存里有人声；没听过就先调用 listen_song。
        什么时候别用：对方在说正事；写代码/列步骤的时候。

        Args:
            start(number): 从人声的第几秒开始，填你听到的副歌开始时间（秒）
            seconds(number): 唱多长，默认 45 秒，最长 60 秒；想唱整首用 full=true
            semitones(number): 升降调，原调填 0；太高/太低可以 ±2、±3
            full(boolean): true=从 start 一直唱到歌曲结尾（最长约 3 分钟），会自动分成多条语音发
        """
        if not self._music_enabled():
            return "（听歌/翻唱现在关着，要用得先去大肥鱼控制台打开）"
        if not self._cfg("enable_sing"):
            return "（唱歌功能关着，别唱，直接用文字回应）"
        info = self.cache.get(event.unified_msg_origin)
        if not info or time.time() - float(info.get("ts") or 0) > 3600:
            return "（手边没有刚听过的歌，唱不了；直接跟她说话）"
        vocals = info.get("vocals") or ""
        if not vocals or not os.path.isfile(vocals):
            return "（没找到刚才那首歌的人声，唱不了）"

        await self._free_for_music()
        start = max(0.0, float(start or 0))
        total = await self._probe(vocals)
        if full:
            seconds = max(4.0, total - start)
            seconds = min(seconds, float(self._cfg("max_full_sing_seconds") or 240))
        else:
            limit = min(60.0, float(self._cfg("max_sing_seconds") or 60))
            seconds = min(max(4.0, float(seconds or 45)), limit)
        if seconds > 30:
            try:
                await event.send(MessageChain([Plain(f"（唱 {seconds:.0f} 秒，需要等一会儿…）")]))
            except Exception:  # noqa: BLE001
                pass
        await self._ensure_f16_ref()
        try:
            wav = await self._sing(vocals, start, seconds, int(semitones or 0))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu-music] 唱失败：{e}")
            await self._restore_voice()
            return "（这次没唱出来，别硬说唱了；可以用文字哼两句或者吐槽一下）"

        sent = await self._send_singing(event, wav)
        if not sent:
            await self._restore_voice()
            return "（语音条没发出去，别硬说唱了）"
        logger.info(f"[dafeiyu-music] 唱了 {seconds:.0f} 秒，发了 {sent} 条")
        await self._restore_voice()
        return (f"（已经用你的声音唱了 {sent} 条语音发过去，共约 {seconds:.0f} 秒。"
                "不要再打字重复歌词，简单说一句就结束这轮）")
