"""大肥鱼·联网

给她两个工具：
  web_search  搜一下
  web_page    打开一个网页，把正文读出来

搜索后端有两条腿：
  1. TinyFish（api.search.tinyfish.ai / api.fetch.tinyfish.ai）——主用。
     结果带标题、链接、摘要，还能把整页抽成 markdown，快且干净。
     key 从环境变量 TINYFISH_API_KEY 或本机密钥文件读，**不写进配置、不进仓库**。
  2. Bing 的 RSS 口（cn.bing.com/search?...&format=rss）——TinyFish 挂了/没 key 时兜底。
     不用注册、没有额度，返回干净 XML。

为什么要自己写：AstrBot 自带的 web_search 要配 tavily / bocha / exa 之类的
付费或需注册的 key，没 key 就直接不挂工具，所以她一直「上不了网」。

注意：这是给聊天用的轻量联网，别拿它扒大站，也别一次抓几千字塞爆上下文。
"""

import asyncio
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import uuid
import wave
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import ImageURLPart, TextPart

try:  # 统一解析 B站/抖音/贴吧/小黑盒 等链接（MIT）
    from parsehub import ParseHub
    from parsehub.types import PostType
except Exception:  # noqa: BLE001
    ParseHub = None
    PostType = None


try:  # 小黑盒 / 抖音的页面是 JS 渲染的，需要从 DOM 里抽正文
    from bs4 import BeautifulSoup
except Exception:  # noqa: BLE001
    BeautifulSoup = None

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
BING_CN = "https://cn.bing.com/search"
BING_COM = "https://www.bing.com/search"
TINYFISH_SEARCH = "https://api.search.tinyfish.ai"
TINYFISH_FETCH = "https://api.fetch.tinyfish.ai"

ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
CDATA_RE = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.S)
SCRIPT_RE = re.compile(r"<(script|style|noscript|svg|iframe)\b.*?</\1>", re.S | re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
BLOCK_RE = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|header|footer|ul|ol|table)\b[^>]*>", re.I)
TAG_RE = re.compile(r"<[^>]+>")
HSPACE_RE = re.compile(r"[ \t\u00a0\u3000]+")
VSPACE_RE = re.compile(r"\n\s*\n\s*\n+")
MD_MARKS_RE = re.compile(r"^[#>\-\*\|\s:]+|[*_`]{1,3}")

MAX_SNIPPET = 220
MAX_PAGE_CHARS = 2600

# ---------------------------------------------------------------- 链接阅读
ROOT = Path(__file__).resolve().parents[4]
LINK_TMP = ROOT / "astrbot" / "data" / "temp" / "link_reader"
FFMPEG = ROOT / "ffmpeg" / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
ASR_DIR = ROOT / "asr"
LINK_RE = re.compile(r"https?://[^\s<>\"'，。！？、；：）】」』]+", re.I)
LINK_HOSTS_VIDEO = ("bilibili.com", "b23.tv", "douyin.com", "iesdouyin.com")
LINK_MAX_CHARS = 3200
LINK_MAX_AUDIO = 180          # 最多听前 3 分钟
EDGE_CDP_PORT = 9223
EDGE_CDP_BASE = f"http://127.0.0.1:{EDGE_CDP_PORT}"
_CDP_COOKIE_CACHE: dict[str, tuple[float, str]] = {}


def _link_host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).netloc or "").lower().split(":")[0]
    except Exception:  # noqa: BLE001
        return ""


def _is_video_link(url: str) -> bool:
    host = _link_host(url)
    return any(host == h or host.endswith("." + h) for h in LINK_HOSTS_VIDEO)


def _ffmpeg_path() -> str:
    if FFMPEG.is_file():
        return str(FFMPEG)
    return shutil.which("ffmpeg") or ""


def _asr_ready() -> bool:
    return all((ASR_DIR / n).is_file() for n in ("sherpa-onnx-offline.exe", "model.int8.onnx", "tokens.txt"))


def _skip_link(url: str) -> bool:
    """QQ 文件、图片/音视频直链不读。"""
    host = _link_host(url)
    if not host:
        return True
    if any(host == h or host.endswith("." + h)
           for h in ("qq.com", "url.cn", "qpic.cn", "gtimg.cn", "bilibili.io")):
        return True
    path = (urllib.parse.urlparse(url).path or "").lower()
    return path.endswith((
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
        ".mp4", ".flv", ".mkv", ".mov", ".avi", ".webm",
        ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".silk",
        ".zip", ".rar", ".7z", ".apk", ".exe", ".dll", ".pdf",
    ))


def _meta(dom: str, *names: str) -> str:
    for name in names:
        for pat in (
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\'](.*?)["\']',
            rf'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:property|name)=["\']{re.escape(name)}["\']',
        ):
            m = re.search(pat, dom, re.S | re.I)
            if m:
                return html_lib.unescape(_squash(m.group(1)))[:600]
    return ""


def _page_title(dom: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", dom, re.S | re.I)
    return html_lib.unescape(_squash(m.group(1))) if m else ""


def _ok_image(url: str) -> bool:
    u = (url or "").strip().lower()
    return u.startswith(("http://", "https://"))


def _fmt_duration(sec: float) -> str:
    try:
        sec = int(float(sec or 0))
    except Exception:  # noqa: BLE001
        return ""
    if sec <= 0:
        return ""
    return f"{sec // 60}:{sec % 60:02d}"

# 密钥文件候选位置：家里的隐藏目录优先（不在项目目录里，不会被任何同步/
# 备份/仓库顺手带走），Windows 上再补一个 %LOCALAPPDATA%，手机上是 /root。
KEY_PATHS = [
    Path.home() / ".dafeiyu" / "tinyfish.key",
]
if os.name == "nt":
    _lad = os.environ.get("LOCALAPPDATA")
    if _lad:
        KEY_PATHS.append(Path(_lad) / "dafeiyu" / "tinyfish.key")
else:
    KEY_PATHS.append(Path("/root/.dafeiyu/tinyfish.key"))

_KEY_CACHE = {"value": "", "at": 0.0}


def _read_key() -> str:
    """按 环境变量 → 密钥文件 的顺序找 TinyFish key。找不到返回空串。"""
    env_key = (os.environ.get("TINYFISH_API_KEY") or "").strip()
    if env_key:
        return env_key
    now = time.time()
    if _KEY_CACHE["value"] and now - _KEY_CACHE["at"] < 60:
        return _KEY_CACHE["value"]
    for path in KEY_PATHS:
        try:
            if path.is_file():
                val = path.read_text(encoding="utf-8").strip()
                if val:
                    _KEY_CACHE.update({"value": val, "at": now})
                    return val
        except OSError:
            continue
    _KEY_CACHE.update({"value": "", "at": now})
    return ""


def _field(blob: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", blob, re.S | re.I)
    if not m:
        return ""
    val = m.group(1).strip()
    cd = CDATA_RE.match(val)
    if cd:
        val = cd.group(1)
    return html_lib.unescape(val).strip()


def _clean_query(query: str) -> str:
    return " ".join(str(query or "").split())[:200]


def _squash(text: str) -> str:
    text = HSPACE_RE.sub(" ", text)
    text = VSPACE_RE.sub("\n\n", text)
    return text.strip()


async def _get(url: str, timeout: float = 15.0, limit: int = 900_000) -> str:
    """取一个 URL 的文本。GBK 页面也能吃。"""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
        async with sess.get(url, headers=HEADERS, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            raw = await resp.content.read(limit)
            charset = (resp.charset or "").lower()
            for enc in [charset, "utf-8", "gb18030"]:
                if not enc:
                    continue
                try:
                    return raw.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return raw.decode("utf-8", "ignore")


# ---------------------------------------------------------------- TinyFish


async def _tinyfish_search(query: str, count: int) -> list[dict]:
    """TinyFish 搜索。meta 里不要出现 key，出错也只会报状态码。"""
    key = _read_key()
    if not key:
        raise RuntimeError("没有 TinyFish key")
    base = {"query": query, "location": "CN", "language": "zh"}
    headers = {"X-API-Key": key, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        for params in (base, {"query": query}):
            url = TINYFISH_SEARCH + "?" + urllib.parse.urlencode(params)
            async with sess.get(url, headers=headers) as resp:
                # 400/422 = 它不认 location/language，去掉重试一次
                if resp.status in (400, 422) and params is base:
                    continue
                if resp.status != 200:
                    raise RuntimeError(f"TinyFish search HTTP {resp.status}")
                data = await resp.json(content_type=None)

    out = []
    for item in (data.get("results") or [])[: max(1, count)]:
        link = (item.get("url") or "").strip()
        if not link:
            continue
        title = (item.get("title") or "").strip() or link
        out.append(
            {
                "title": title,
                "url": link,
                "snippet": (item.get("snippet") or "").strip(),
                "site": (item.get("site_name") or "").strip(),
            }
        )
    return out


async def _tinyfish_fetch(url: str) -> str:
    """TinyFish 把网页抽成 markdown。失败抛异常。"""
    key = _read_key()
    if not key:
        raise RuntimeError("没有 TinyFish key")
    headers = {
        "X-API-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"urls": [url], "format": "markdown"}
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(TINYFISH_FETCH, headers=headers, json=body) as resp:
            if resp.status != 200:
                raise RuntimeError(f"TinyFish fetch HTTP {resp.status}")
            data = await resp.json(content_type=None)
    results = data.get("results") or []
    if results:
        text = results[0].get("text") or ""
        if text.strip():
            return text
    reason = ""
    for err in data.get("errors") or []:
        if err.get("url") == url and err.get("error"):
            reason = str(err["error"])
            break
    raise RuntimeError(f"TinyFish 没抽到正文{f'（{reason}）' if reason else ''}")


# ---------------------------------------------------------------- Bing 兜底


async def _bing(query: str, count: int, base: str) -> list[dict]:
    params = {"q": query, "format": "rss", "setlang": "zh-Hans"}
    if base is BING_CN or "cn.bing" in base:
        params["cc"] = "cn"
    url = base + "?" + urllib.parse.urlencode(params)
    text = await _get(url)
    out = []
    for blob in ITEM_RE.findall(text):
        link = _field(blob, "link")
        title = _field(blob, "title")
        if not link or not title:
            continue
        out.append(
            {
                "title": title,
                "url": link,
                "snippet": _field(blob, "description"),
                "site": "",
            }
        )
        if len(out) >= count:
            break
    return out


async def search_web(query: str, count: int = 5) -> tuple[list[dict], str]:
    """搜索。返回 (结果, 用了哪条腿)。TinyFish 优先，失败退 Bing。"""
    errors = []
    try:
        items = await _tinyfish_search(query, count)
        if items:
            return items, "tinyfish"
        errors.append("TinyFish 没有结果")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"TinyFish {exc}")

    for base in (BING_CN, BING_COM):
        try:
            items = await _bing(query, count, base)
            if items:
                return items, "bing"
            errors.append(f"{base} 没有结果")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{base} {exc}")
    if errors:
        # 只记状态，不记 key
        logger.warning("[dafeiyu-web] 搜索失败: " + " / ".join(errors))
    return [], "none"


# ---------------------------------------------------------------- 正文清洗


def html_to_text(raw: str) -> str:
    text = COMMENT_RE.sub(" ", raw)
    text = SCRIPT_RE.sub(" ", text)
    text = BLOCK_RE.sub("\n", text)
    text = TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    return _lines_to_text(_squash(text))


def markdown_to_text(md: str) -> str:
    """TinyFish 给的 markdown 也照样清一遍：去标记、去短行、去重复。"""
    return _lines_to_text(_squash(md))


def _lines_to_text(text: str) -> str:
    # 导航栏、按钮、面包屑大多是一两个字的短行，正文行不会这么短；
    # 顺手把紧挨着的重复行（同一段被切两遍）也去掉。
    kept = []
    for line in text.split("\n"):
        line = MD_MARKS_RE.sub("", line).strip()
        if len(line) < 5:
            continue
        if kept and kept[-1] == line:
            continue
        kept.append(line)
    return "\n".join(kept)


# ---------------------------------------------------------------- 统一链接解析
# B站 / 抖音 / 贴吧 / 小黑盒 交给 ParseHub（MIT）：
# https://github.com/z-mio/ParseHub
# 它把各站点的签名、移动端兜底和正文抽取都封装好了；这里只负责补上
# “视频里的声音”转文字（本地 sherpa-onnx）和给模型看的简短笔记。


async def _run_cmd(cmd: list[str], timeout: float = 60,
                   cwd: str | None = None) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
            await p.wait()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("命令超时")
    return (p.returncode or 0,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"))


async def _asr_audio(audio: str, work: Path, max_seconds: int = LINK_MAX_AUDIO) -> tuple[str, str]:
    """把视频里的声音前几分钟转成文字。返回 (文字, 失败原因)。"""
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return "", "没找到 ffmpeg"
    if not _asr_ready():
        return "", "本机没装语音识别"
    adir = work / "asr"
    adir.mkdir(parents=True, exist_ok=True)
    wav = work / "speech.wav"
    code, _, err = await _run_cmd([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-t", str(int(max_seconds)),
        "-i", audio, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(wav),
    ], timeout=90)
    if code != 0 or not wav.is_file() or wav.stat().st_size < 1000:
        return "", f"音频转码失败：{err.strip()[-120:]}"
    code, _, err = await _run_cmd([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(wav),
        "-af", "adelay=500:all=1,apad=pad_dur=0.5", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "segment", "-segment_time", "15",
        "-y", str(adir / "asr_%03d.wav"),
    ], timeout=90)
    chunks = sorted(adir.glob("asr_*.wav"))
    if code != 0 or not chunks:
        return "", f"切音频失败：{err.strip()[-120:]}"
    # sherpa-onnx 的 argv 解析对中文绝对路径不友好；切到 asr 目录、传相对路径
    exe = ASR_DIR / "sherpa-onnx-offline.exe"
    cmd = [
        str(exe), "--num-threads=4", "--tokens=tokens.txt",
        "--sense-voice-model=model.int8.onnx",
        "--sense-voice-use-itn=1", "--sense-voice-language=zh",
        *[os.path.relpath(c, ASR_DIR) for c in chunks],
    ]
    code, out, err = await _run_cmd(cmd, timeout=90 + 20 * len(chunks),
                                    cwd=str(ASR_DIR))
    text = ""
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            seg = str(json.loads(line).get("text") or "").strip()
        except ValueError:
            continue
        if seg:
            text += ("" if not text or text[-1] in "，。！？、,.!?…" else "，") + seg
    if not text:
        return "", f"没认出字：{err.strip()[-120:]}"
    return text, ""


async def _media_audio(url: str, work: Path, referer: str = "",
                       max_seconds: int = LINK_MAX_AUDIO) -> tuple[str, str]:
    """从视频直链里抽前几分钟音频；不下载完整视频。"""
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return "", "没找到 ffmpeg"
    out = work / "video_audio.wav"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if referer:
        cmd += ["-headers", f"Referer: {referer}\r\nUser-Agent: {UA}\r\n"]
    cmd += ["-t", str(int(max_seconds)), "-i", url, "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-y", str(out)]
    code, _, err = await _run_cmd(cmd, timeout=120)
    if code != 0 or not out.is_file() or out.stat().st_size < 1000:
        return "", f"视频流下载失败：{err.strip()[-120:]}"
    return str(out), ""


COOKIE_FILES = [
    ROOT / "private" / "edge-cookies.txt",
    ROOT / "private" / "cookies.txt",
    ROOT / "private" / "parsehub_cookies.json",
]


def _cookie_domain_match(domain: str, host: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    h = (host or "").lower()
    return bool(d and h) and (h == d or h.endswith("." + d))


def _cookie_file_header(url: str) -> str:
    """从 private/ 下读取 Edge 导出的 cookies.txt / JSON，按域名挑出 Cookie 头。

    只读本机文件，不打印内容。支持：
    - Netscape cookies.txt（Get cookies.txt LOCALLY / Cookie-Editor 导出）
    - Cookie-Editor 的 JSON 数组
    - {"douyin": "...", "bilibili": "..."} 这种平台 -> Cookie 字符串
    """
    host = _link_host(url)
    if not host:
        return ""
    for path in COOKIE_FILES:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if path.suffix.lower() == ".json":
            try:
                data = json.loads(text)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict):
                for key, val in data.items():
                    if isinstance(val, str) and _cookie_domain_match(str(key), host):
                        return val.strip()
                if all(isinstance(v, str) for v in data.values()):
                    return "; ".join(f"{k}={v}" for k, v in data.items())
            elif isinstance(data, list):
                found = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    if _cookie_domain_match(str(item.get("domain") or ""), host):
                        name = str(item.get("name") or "")
                        value = str(item.get("value") or "")
                        if name:
                            found.append(f"{name}={value}")
                if found:
                    return "; ".join(found)
            continue

        found = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _flag, _path, _secure, _exp, name, value = parts[:7]
            if name and _cookie_domain_match(domain, host):
                found.append(f"{name}={value}")
        if found:
            return "; ".join(found)
    return ""


async def _cdp_cookie_header(url: str) -> str:
    """从专用 Edge 调试实例读 Cookie（浏览器自己解密，不碰 Edge 数据库）。

    控制台「链接登录」启动的 Edge 带 --remote-debugging-port=9223，
    这里连它的浏览器级 WebSocket，调 Storage.getCookies。
    """
    host = _link_host(url)
    if not host:
        return ""
    now = time.time()
    hit = _CDP_COOKIE_CACHE.get(host)
    if hit and now - hit[0] < 60:
        return hit[1]
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(EDGE_CDP_BASE + "/json/version") as resp:
                if resp.status != 200:
                    return ""
                ver = await resp.json(content_type=None)
            ws_url = str(ver.get("webSocketDebuggerUrl") or "")
            if not ws_url:
                return ""
            async with sess.ws_connect(ws_url, timeout=4) as ws:
                await ws.send_json({"id": 1, "method": "Storage.getCookies"})
                while True:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=4)
                    if msg.get("id") != 1:
                        continue
                    cookies = (msg.get("result") or {}).get("cookies") or []
                    break
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[dafeiyu-web] CDP Cookie 没读到: {type(exc).__name__}: {exc}")
        return ""
    found = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        if not _cookie_domain_match(str(c.get("domain") or ""), host):
            continue
        exp = c.get("expires")
        try:
            if exp and float(exp) > 0 and float(exp) < now:
                continue
        except Exception:  # noqa: BLE001
            pass
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        if name:
            found.append(f"{name}={value}")
    header = "; ".join(found)
    _CDP_COOKIE_CACHE[host] = (now, header)
    return header


async def _cookie_header(url: str) -> str:
    header = await _cdp_cookie_header(url)
    return header or _cookie_file_header(url)


def _media_urls(res) -> tuple[str, str, str, float]:
    """从 ParseHub 结果里取 (视频直链, 封面, 图片, 时长)。"""
    media = getattr(res, "media", None)
    items = list(media) if isinstance(media, (list, tuple)) else ([media] if media else [])
    video = cover = image = ""
    duration = 0.0
    for item in items:
        u = str(getattr(item, "url", "") or "")
        thumb = str(getattr(item, "thumb_url", "") or "")
        cls = type(item).__name__.lower()
        if "video" in cls or "livephoto" in cls:
            if not video:
                video = u
            if not cover:
                cover = thumb
            try:
                duration = float(getattr(item, "duration", 0) or 0)
            except Exception:  # noqa: BLE001
                pass
        elif "image" in cls or "ani" in cls:
            if not image:
                image = u
            if not cover:
                cover = thumb or u
    return video, cover, image, duration


async def _parsehub_read(url: str, work: Path) -> dict:
    if ParseHub is None:
        raise RuntimeError("没装 ParseHub")
    ph = ParseHub()
    cookie = await _cookie_header(url)
    if cookie:
        logger.info(f"[dafeiyu-web] 使用本机 {_link_host(url)} 的 Cookie 解析")
    res = await asyncio.wait_for(ph.parse(url, cookie=cookie or None), timeout=45)
    platform = str(getattr(res, "platform", "") or "")
    title = str(getattr(res, "title", "") or "").strip()
    content = str(getattr(res, "content", "") or "").strip()
    if not content:
        content = str(getattr(res, "markdown_content", "") or "").strip()
    video, cover, image, duration = _media_urls(res)
    # 抖音的 duration 是毫秒；B站一般是秒
    if ("douyin" in platform.lower() or "抖音" in platform) and duration > 1000:
        duration = duration / 1000.0
    transcript, why = "", ""
    is_video = bool(video) or str(getattr(res, "type", "")).lower().endswith("video")
    if video:
        ref = ""
        if "bilibili" in platform.lower() or "B站" in platform:
            ref = "https://www.bilibili.com/"
        elif "抖音" in platform or "douyin" in platform.lower():
            ref = "https://www.douyin.com/"
        elif "小黑盒" in platform:
            ref = "https://www.xiaoheihe.cn/"
        audio, err = await _media_audio(video, work, referer=ref)
        if audio:
            transcript, why = await _asr_audio(audio, work)
        else:
            why = err
    return {
        "site": platform or _link_host(url),
        "title": title,
        "text": content,
        "transcript": transcript,
        "asr_error": why,
        "cover": cover or image,
        "duration": _fmt_duration(duration),
        "duration_sec": duration,
        "video": is_video,
        "url": url,
    }


async def _generic_read(url: str, work: Path) -> dict:
    raw = ""
    text = ""
    try:
        text = markdown_to_text(await _tinyfish_fetch(url))
    except Exception:  # noqa: BLE001
        pass
    if len(text) < 100:
        try:
            raw = await _get(url, timeout=20)
            text = html_to_text(raw)
        except Exception:  # noqa: BLE001
            pass
    if len(text) < 40 and not raw:
        raise RuntimeError("网页没有可读正文")
    title = _page_title(raw) if raw else ""
    desc = _meta(raw, "description", "og:description") if raw else ""
    return {"site": _link_host(url), "title": title, "text": text or desc,
            "cover": _meta(raw, "og:image", "twitter:image") if raw else "", "url": url}


async def read_link(url: str, work: Path) -> dict:
    if ParseHub is not None:
        try:
            return await _parsehub_read(url, work)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[dafeiyu-web] ParseHub 解析失败 {url}: {type(exc).__name__}: {exc}")
            host = _link_host(url)
            if any(h in host for h in ("bilibili", "douyin", "xiaoheihe", "tieba")):
                return {"site": host, "blocked": True,
                        "reason": f"解析失败：{str(exc)[:100]}", "url": url}
    return await _generic_read(url, work)


def format_link_note(info: dict, tool: bool = False) -> str:
    if info.get("blocked"):
        return ("[对方发来的链接打不开：" + str(info.get("reason") or "被站点挡住了") +
                "。你看不到内容，别装看过；直接说打不开，让她复制正文或发截图。]")
    head = "网页内容" if tool else "对方发来一个链接，你刚打开看过"
    parts = [f"[{head}。站点：{info.get('site') or '网页'}"]
    if info.get("title"):
        parts.append("标题：" + str(info["title"])[:200])
    if info.get("duration"):
        parts.append("时长：" + str(info["duration"]))
    if info.get("text"):
        parts.append("简介/正文：\n" + str(info["text"])[:1800])
    if info.get("transcript"):
        parts.append("视频里说的话（自动转写，可能有错字，别把这段照念）：\n"
                     + str(info["transcript"])[:1800])
    elif info.get("video") and info.get("asr_error"):
        parts.append("（声音没读出来：" + str(info["asr_error"])[:100] + "）")
    if info.get("video"):
        parts.append("（画面只看了封面，没有逐帧看；别编画面细节，没听到的就说没听清。）")
    note = "\n".join(parts)
    if len(note) > LINK_MAX_CHARS:
        note = note[:LINK_MAX_CHARS] + "……（后面还有，只读到这里）"
    if tool:
        return note + "\n（用你自己的话回应，别整段照搬。）]"
    return note + "\n（内容已经读给你了，不用再调 web_page；用你自己的话回应。）]"


class DafeiyuWeb(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._link_cache: dict[str, tuple[float, dict]] = {}
        self._link_lock = asyncio.Lock()
        self._cookie_mtime = 0.0
        if _read_key():
            logger.info("[dafeiyu-web] TinyFish key 已就位，搜索走 TinyFish（Bing 兜底）")
        else:
            logger.warning("[dafeiyu-web] 没读到 TinyFish key，只用 Bing 的 RSS 结果")
        if ParseHub is not None:
            logger.info("[dafeiyu-web] 链接阅读：ParseHub 已就位（B站/抖音/贴吧/小黑盒）")
        else:
            logger.warning("[dafeiyu-web] 没装 ParseHub，链接只能走普通网页抓取")
        if _asr_ready():
            logger.info("[dafeiyu-web] 链接阅读：本机语音识别已就位")

    def _cookie_stamp(self) -> float:
        stamps = []
        for path in COOKIE_FILES:
            try:
                if path.is_file():
                    stamps.append(path.stat().st_mtime)
            except OSError:
                pass
        return max(stamps) if stamps else 0.0

    async def _read_cached(self, url: str) -> dict:
        now = time.time()
        stamp = self._cookie_stamp()
        if stamp != self._cookie_mtime:
            self._cookie_mtime = stamp
            self._link_cache.clear()
        hit = self._link_cache.get(url)
        if hit and now - hit[0] < 1800:
            return hit[1]
        LINK_TMP.mkdir(parents=True, exist_ok=True)
        work = LINK_TMP / ("job_" + uuid.uuid4().hex[:10])
        work.mkdir(parents=True, exist_ok=True)
        try:
            async with self._link_lock:
                info = await asyncio.wait_for(read_link(url, work), timeout=150)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if not info.get("blocked"):
            self._link_cache[url] = (now, info)
        if len(self._link_cache) > 24:
            for key, _ in sorted(self._link_cache.items(), key=lambda kv: kv[1][0])[:8]:
                self._link_cache.pop(key, None)
        return info

    @filter.on_llm_request()
    async def auto_read_links(self, event: AstrMessageEvent, req):
        """对方发链接时先替她打开看一眼，把内容作为临时片段交给模型。

        临时片段带 mark_as_temp，不会写进聊天历史；链接内容也不反复占上下文。
        """
        try:
            text = event.message_str or ""
        except Exception:  # noqa: BLE001
            return
        urls = []
        for raw in LINK_RE.findall(text):
            u = raw.rstrip(".,;:!?)]}，。；：！？）】」』")
            if u and not _skip_link(u) and u not in urls:
                urls.append(u)
        if not urls:
            return
        if not event.is_private_chat():
            try:
                if not event.is_at_or_wake_command:
                    return
            except Exception:  # noqa: BLE001
                return
        url = urls[0]
        try:
            hit = self._link_cache.get(url)
            if not hit or time.time() - hit[0] >= 1800:
                if _is_video_link(url):
                    try:
                        await event.send(MessageChain([Plain("（我点开这个链接看看…）")]))
                    except Exception:  # noqa: BLE001
                        pass
                logger.info(f"[dafeiyu-web] 打开链接：{url}")
            info = await self._read_cached(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dafeiyu-web] 链接读取失败 {url}: {type(exc).__name__}: {exc}")
            info = {"blocked": True, "reason": f"打不开（{type(exc).__name__}）", "url": url}

        note = format_link_note(info)
        try:
            req.extra_user_content_parts.append(TextPart(text=note).mark_as_temp())
        except Exception:  # noqa: BLE001
            return
        cover = str(info.get("cover") or "")
        if _ok_image(cover):
            try:
                req.extra_user_content_parts.append(
                    ImageURLPart(image_url=ImageURLPart.ImageURL(url=cover)).mark_as_temp())
            except Exception:  # noqa: BLE001
                pass
        logger.info(
            f"[dafeiyu-web] 链接 {url} → {info.get('site', '?')} "
            f"{'已读' if not info.get('blocked') else '失败'}"
            f"（{len(note)} 字）")

    @filter.llm_tool(name="web_search")
    async def web_search(self, event: AstrMessageEvent, query: str) -> str:
        """上网搜一下，拿回几条网页结果（标题 + 链接 + 摘要）。

        什么时候用：她答不上来的实时信息——今天天气、最近新闻、某部剧/游戏/事件的
        最新情况、某个词/人名/梗的意思、需要查资料或核实数字。
        什么时候别用：闲聊、撒娇、她已经知道的事、纯情绪回应——那些直接说话就行。
        搜完自己消化成人话讲给对方听，别把链接和摘要原样贴出去；确实需要给出处时
        再挑一条链接。

        Args:
            query(string): 搜索词。用短句或关键词，比如「杭州今天天气」「哪吒2 票房」
        """
        q = _clean_query(query)
        if not q:
            return "（搜索词是空的，换个说法再搜）"
        try:
            items, via = await search_web(q, count=5)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[dafeiyu-web] 搜索异常: {exc}")
            return "（搜索没成功，网络可能不太顺。先按你知道的回答，实在不知道就说不知道）"
        if not items:
            return f"（「{q}」没搜到结果。换个说法，或者直接按你知道的说）"

        lines = [f"「{q}」的搜索结果："]
        for idx, item in enumerate(items, 1):
            snippet = _squash(item["snippet"])[:MAX_SNIPPET]
            site = item.get("site") or ""
            head = f"{idx}. {item['title']}" + (f"（{site}）" if site else "")
            lines.append(f"{head}\n   {item['url']}\n   {snippet}")
        lines.append("（以上是搜索摘要，挑有用的揉成自己的话讲；别整段照抄，也别说「根据搜索结果」）")
        logger.info(f"[dafeiyu-web] 搜索 {q!r} → {via} 命中 {len(items)} 条")
        return "\n".join(lines)

    @filter.llm_tool(name="web_page")
    async def web_page(self, event: AstrMessageEvent, url: str) -> str:
        """打开一个网页，把里面的正文读出来（纯文字）。

        什么时候用：对方发了个链接让她看、搜索结果里有篇需要细读的页面、
        想知道某个页面上到底写了什么。B站/抖音视频会读标题、简介，并尽量把
        视频里的说话转成文字；小黑盒、贴吧等站点也会尝试渲染后读取。
        什么时候别用：只想看看某站首页长什么样、已经在搜索结果摘要里能答上的问题。

        Args:
            url(string): 完整网址，要以 http:// 或 https:// 开头
        """
        target = str(url or "").strip()
        if not target.lower().startswith(("http://", "https://")):
            return "（这不是一个完整网址，需要以 http:// 或 https:// 开头）"

        try:
            info = await self._read_cached(target)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dafeiyu-web] 打开失败 {target}: {exc}")
            return f"（这个网页打不开：{exc}。可以换个链接，或者直接说打不开）"
        return format_link_note(info, tool=True)