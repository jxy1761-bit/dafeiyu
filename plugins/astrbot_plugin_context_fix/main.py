"""大肥鱼·上下文清洗（瘦身）

每次请求发给模型之前，给 `req.contexts` 洗一遍：

1. **历史图片不再重复上传**（`strip_images`）
   你发过的图，模型当轮已经看过了。但 AstrBot 会把那段 history 原样带上，
   一张 86KB 的 jpeg base64 就是一两万 token，几张图就把上下文顶爆。
   这里把历史上 **user 消息里的图片块** 换成一行文字占位，当前这一轮的图
   （走 `req.image_urls`，不在 contexts 里）不受影响。

2. **丢掉 think 块**（`drop_think`）
   AstrBot 把推理内容当 `{"type": "think"}` 存进历史，小米 MiMo 这类严格
   OpenAI 兼容接口不认，会 400 `Invalid request parameters`。

3. **修坏图片地址**（`fix_image`）
   老图的 `image_url.url` 落库时被抹成了 `"omitted"`，严格接口会报
   `only base64, http or https URLs are supported`。

4. **听歌笔记不再反复占上下文**（`strip_music`）
   听歌插件会往当前用户消息追加一段听歌笔记（含歌词）。以前这笔记会被
   AstrBot 原样存进 history，之后每一轮都重发一遍。这里把历史里的笔记
   压成一行占位；当前这一轮刚听的歌不受影响（它走 extra parts，且已标记
   为临时内容，不会再落库）。

5. **自动上下文压缩**（`compress_enable`）
   历史太长时，保留最近若干条原样，把更早的 user/assistant 文本压成短摘要
   占位、把旧工具结果换成一行「已省略」。只处理请求里的 contexts，不改
   记忆插件的召回内容；长期记忆仍由 LivingMemory 自己的库负责。

只改 `req.contexts`，不动数据库、不换模型、不碰内核。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.provider.entities import ProviderRequest

PLUGIN_NAME = "astrbot_plugin_context_fix"

# 合法图片地址：data: / http / https，其余一律视为坏块
OK_PREFIX = ("data:", "http://", "https://")

# OpenAI 兼容接口认识的块类型，其余（think 等）一律清理
KEEP_TYPES = {"text", "image_url", "image", "audio_url", "audio", "input_audio"}

IMAGE_TYPES = ("image_url", "image")

DEFAULTS = {
    "strip_images": True,
    "fix_image": True,
    "drop_think": True,
    "strip_music": True,
    "compress_enable": True,
    "compress_trigger_chars": 60000,
    "compress_keep_recent": 30,
    "compress_old_chars": 160,
    "compress_tool_chars": 220,
    "placeholder": "[之前发过的图片，你看过了，内容已省略]",
    "log_every": 60,
}

MUSIC_PREFIX = "[之前听过一首歌"
COMPRESS_MARK = "〔较早内容已压缩〕"
TOOL_OMIT_MARK = "[较早的工具结果已省略，原 {n} 字]"


def _music_digest(text: str) -> str:
    """把一条听歌笔记压成很短的一行：留一点风格/情绪，歌词和分析都丢掉。"""
    t = " ".join(str(text or "").split())
    for prefix in (
        "你听到的内容：",
        "[对方发来一段带音乐的视频/音频，你刚才戴上耳机认真听了一遍。",
        "你听到的是：",
        "你听到的内容:",
    ):
        if t.startswith(prefix):
            t = t[len(prefix):].lstrip()
    return f"{MUSIC_PREFIX}；要点：{t[:110]}…（歌词和详细分析已省略）]"


def _is_music_note(text: str) -> bool:
    """识别听歌插件写进历史的那段笔记，避免误伤普通聊天。"""
    if not isinstance(text, str) or len(text) < 40:
        return False
    t = text.strip()
    return (
        ("对方发来一段带音乐的视频/音频" in t and "戴上耳机" in t)
        or ("就当你真的听过这首歌" in t)
        or t.startswith("你听到的内容：")
    )


def _short_text(text: str, limit: int) -> str:
    if not isinstance(text, str) or len(text) <= limit:
        return text
    if text.rstrip().endswith(COMPRESS_MARK):
        return text
    return text[:limit].rstrip() + " …" + COMPRESS_MARK


def context_chars(contexts: list[Any]) -> int:
    """粗略估上下文大小：只数文本，够用来定压缩时机。"""
    total = 0
    for msg in contexts or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += len(str(part.get("text") or part.get("think") or ""))
    return total


def compress_contexts(
    contexts: list[Any],
    trigger_chars: int = 60000,
    keep_recent: int = 30,
    old_chars: int = 160,
    tool_chars: int = 220,
) -> tuple[list[Any], dict[str, int]]:
    """历史超长时原地瘦身：最近 N 条不动，更早的文本压短、工具结果省略。

    不删消息、不改 role/tool_calls，避免把 tool_call 和 tool 结果拆散。
    """
    stats = {"before": context_chars(contexts), "after": 0, "compressed": 0}
    stats["after"] = stats["before"]
    if trigger_chars <= 0 or stats["before"] <= trigger_chars:
        return contexts, stats

    keep_recent = max(1, int(keep_recent or 30))
    start = max(0, len(contexts) - keep_recent)
    out: list[Any] = []

    for i, msg in enumerate(contexts or []):
        if i >= start or not isinstance(msg, dict):
            out.append(msg)
            continue
        if msg.get("role") in ("system", "_checkpoint"):
            out.append(msg)
            continue

        msg = {**msg}
        role = msg.get("role")
        limit = max(40, int(tool_chars if role == "tool" else old_chars))
        content = msg.get("content")

        if isinstance(content, str):
            if role == "tool" and len(content) > limit:
                msg["content"] = TOOL_OMIT_MARK.format(n=len(content))
                stats["compressed"] += 1
            elif role != "tool" and len(content) > limit:
                msg["content"] = _short_text(content, limit)
                stats["compressed"] += 1

        elif isinstance(content, list):
            parts: list[Any] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text") or "")
                    if role == "tool" and len(text) > limit:
                        parts.append({"type": "text",
                                      "text": TOOL_OMIT_MARK.format(n=len(text))})
                        stats["compressed"] += 1
                    elif role != "tool" and len(text) > limit:
                        parts.append({**part, "text": _short_text(text, limit)})
                        stats["compressed"] += 1
                    else:
                        parts.append(part)
                else:
                    parts.append(part)
            msg["content"] = parts

        out.append(msg)

    stats["after"] = context_chars(out)
    return out, stats


def _url_of(part: dict) -> Any:
    data = part.get("image_url") if "image_url" in part else part.get("image")
    if isinstance(data, dict):
        return data.get("url")
    return data


def _url_ok(url: Any) -> bool:
    return isinstance(url, str) and url.startswith(OK_PREFIX)


def sanitize_contexts(
    contexts: list[Any],
    fix_image: bool = True,
    drop_think: bool = True,
    strip_images: bool = True,
    strip_music: bool = True,
    placeholder: str = "[之前发过的图片，你看过了，内容已省略]",
) -> tuple[list[Any], dict[str, int]]:
    """返回 (清理后的 contexts, 统计)。纯函数，方便离线自测。

    图片只在 **user** 消息里被替换成占位：tool 结果里的图是模型刚拿到的，
    assistant 里的图基本不该出现，都不动。
    """
    stats = {"stripped": 0, "image": 0, "think": 0, "music": 0,
             "unknown": 0, "empty": 0}
    out: list[Any] = []

    for raw in contexts or []:
        msg = raw
        if not isinstance(msg, dict):
            dump = getattr(msg, "model_dump_for_context", None) or getattr(msg, "model_dump", None)
            if callable(dump):
                try:
                    msg = dump()
                except Exception:
                    stats["unknown"] += 1
                    continue
            else:
                stats["unknown"] += 1
                continue

        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, list):
            parts: list[dict] = []
            for part in content:
                if not isinstance(part, dict):
                    if isinstance(part, str) and part.strip():
                        parts.append({"type": "text", "text": part})
                    else:
                        stats["unknown"] += 1
                    continue

                ptype = str(part.get("type") or "").lower()

                if ptype == "think":
                    if drop_think:
                        stats["think"] += 1
                        continue
                    parts.append(part)
                    continue

                if ptype == "text" and strip_music:
                    text = str(part.get("text") or "")
                    if _is_music_note(text):
                        stats["music"] += 1
                        digest = _music_digest(text)
                        # 连续多条笔记只留一行
                        if parts and parts[-1].get("type") == "text" \
                                and str(parts[-1].get("text") or "").startswith(MUSIC_PREFIX):
                            continue
                        parts.append({"type": "text", "text": digest})
                        continue

                if ptype in IMAGE_TYPES:
                    if strip_images and role == "user":
                        # 你已经看过了，别再重复上传
                        stats["stripped"] += 1
                        parts.append({"type": "text", "text": placeholder})
                        continue
                    if fix_image and not _url_ok(_url_of(part)):
                        stats["image"] += 1
                        parts.append({"type": "text", "text": placeholder})
                        continue
                    parts.append(part)
                    continue

                if ptype in KEEP_TYPES:
                    parts.append(part)
                    continue

                # 不认识的块：能当文本就当文本，否则丢
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    stats["unknown"] += 1
                    parts.append({"type": "text", "text": text})
                else:
                    stats["unknown"] += 1

            if not parts:
                # 空了：带了 tool_calls 的消息必须留（否则下面的 tool 消息会成孤儿）
                if not (role == "tool" or msg.get("tool_calls")):
                    stats["empty"] += 1
                    continue
                msg = {**msg, "content": ""}
            else:
                msg = {**msg, "content": parts}
        elif isinstance(content, str):
            if strip_music and _is_music_note(content):
                stats["music"] += 1
                msg = {**msg, "content": _music_digest(content)}
            elif not content.strip():
                # 空内容：tool 消息和带 tool_calls 的 assistant 必须留，其他丢掉
                if not (role == "tool" or msg.get("tool_calls")):
                    stats["empty"] += 1
                    continue
        elif content is None:
            # 空内容：tool 消息和带 tool_calls 的 assistant 必须留，其他丢掉
            if not (role == "tool" or msg.get("tool_calls")):
                stats["empty"] += 1
                continue

        out.append(msg)

    return out, stats


class ContextFix(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config
        self._last_log = 0.0
        self._stripped_total = 0
        self._think_total = 0
        self._music_total = 0
        self._compress_total = 0

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            if self.config is None:
                return default
            return self.config.get(key, default)
        except Exception:
            return default

    # 优先级最低 = 最后执行：别的插件（群聊增强、记忆召回）先改完 context，我们再收尾清理
    @filter.on_llm_request(priority=-1000000)
    async def clean_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        contexts = getattr(req, "contexts", None)
        if not contexts:
            return

        options = {
            "strip_images": bool(self._cfg("strip_images", DEFAULTS["strip_images"])),
            "fix_image": bool(self._cfg("fix_image", DEFAULTS["fix_image"])),
            "drop_think": bool(self._cfg("drop_think", DEFAULTS["drop_think"])),
            "strip_music": bool(self._cfg("strip_music", DEFAULTS["strip_music"])),
            "placeholder": str(self._cfg("placeholder", DEFAULTS["placeholder"]) or DEFAULTS["placeholder"]),
        }

        cleaned, stats = sanitize_contexts(contexts, **options)

        cstats = {"before": context_chars(cleaned), "after": context_chars(cleaned),
                  "compressed": 0}
        if self._cfg("compress_enable", DEFAULTS["compress_enable"]):
            cleaned, cstats = compress_contexts(
                cleaned,
                trigger_chars=int(self._cfg("compress_trigger_chars",
                                            DEFAULTS["compress_trigger_chars"]) or 0),
                keep_recent=int(self._cfg("compress_keep_recent",
                                          DEFAULTS["compress_keep_recent"]) or 30),
                old_chars=int(self._cfg("compress_old_chars",
                                        DEFAULTS["compress_old_chars"]) or 160),
                tool_chars=int(self._cfg("compress_tool_chars",
                                         DEFAULTS["compress_tool_chars"]) or 220),
            )

        if cleaned != contexts:
            req.contexts = cleaned  # type: ignore[assignment]

        touched = (stats["stripped"] + stats["image"] + stats["think"]
                   + stats["music"] + cstats["compressed"])
        if not touched:
            return

        self._stripped_total += stats["stripped"]
        self._think_total += stats["think"]
        self._music_total += stats["music"]
        self._compress_total += cstats["compressed"]
        now = time.time()
        every = float(self._cfg("log_every", DEFAULTS["log_every"]) or 60)
        if now - self._last_log >= every:
            self._last_log = now
            logger.info(
                f"[ctx-fix] 历史瘦身：图片 {stats['stripped']} 张转文字、think {stats['think']} 个、"
                f"坏图 {stats['image']} 个、听歌笔记 {stats['music']} 条、"
                f"压缩 {cstats['compressed']} 条"
                f"（{cstats['before']} → {cstats['after']} 字）"
                f" → 剩 {len(cleaned)} 条消息"
                f"（累计省下 {self._stripped_total} 张图 / {self._think_total} 个推理块 / "
                f"{self._music_total} 条听歌笔记 / {self._compress_total} 条旧消息）"
            )
