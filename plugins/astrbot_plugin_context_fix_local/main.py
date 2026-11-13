"""大肥鱼·上下文清洗（本地模型版）

云端那条线用的是 `astrbot_plugin_context_fix`，阈值按 12 万 token 的模型配的
（60000 字符才触发压缩）。本地大模型只有 32k 上下文，那套阈值等于没开：
上下文涨到 1 万 token 就爆，但字符数还远没到 60000。

这个插件就是同一套逻辑的**本地版**：

1. 图片转占位、丢 think、修坏图、压缩听歌笔记 —— 和云端版一致（本地模型更怕这些）
2. 压缩阈值大幅提前（16000 字符 ≈ 9k token 就动手，云端版是 60000）
3. 多一层**硬上限兜底**：压完仍然超过 `hard_limit_chars` 就继续加狠
   （少留最近消息、旧消息留更短），还不够就整条丢弃最早的对话，直到装得下。
   这是本地版独有的 —— 云端模型有 12 万 token，怎么压都不会 400，本地会。

和云端版**互斥**：控制台切模型时会自动开一个关另一个，不要两个同时开。

只改 `req.contexts`，不动数据库、不换模型、不碰内核。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.provider.entities import ProviderRequest

PLUGIN_NAME = "astrbot_plugin_context_fix_local"
TAG = "[ctx-fix-local]"

# 合法图片地址：data: / http / https，其余一律视为坏块
OK_PREFIX = ("data:", "http://", "https://")

# OpenAI 兼容接口认识的块类型，其余（think 等）一律清理
KEEP_TYPES = {"text", "image_url", "image", "audio_url", "audio", "input_audio"}

IMAGE_TYPES = ("image_url", "image")

DEFAULTS = {
    "enabled": True,
    "strip_images": True,
    "fix_image": True,
    "drop_think": True,
    "strip_music": True,
    "compress_enable": True,
    "compress_trigger_chars": 16000,
    "compress_keep_recent": 12,
    "compress_old_chars": 90,
    "compress_tool_chars": 120,
    "hard_limit_chars": 26000,
    "min_keep_messages": 6,
    "placeholder": "[之前发过的图片，你看过了，内容已省略]",
    "log_every": 60,
}

MUSIC_PREFIX = "[之前听过一首歌"
COMPRESS_MARK = "〔较早内容已压缩〕"
TOOL_OMIT_MARK = "[较早的工具结果已省略，原 {n} 字]"
DROPPED_MARK = "[更早的对话已按本地上下文上限丢弃]"


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
    trigger_chars: int = 16000,
    keep_recent: int = 12,
    old_chars: int = 90,
    tool_chars: int = 120,
) -> tuple[list[Any], dict[str, int]]:
    """历史超长时原地瘦身：最近 N 条不动，更早的文本压短、工具结果省略。

    不删消息、不改 role/tool_calls，避免把 tool_call 和 tool 结果拆散。
    """
    stats = {"before": context_chars(contexts), "after": 0, "compressed": 0}
    stats["after"] = stats["before"]
    if trigger_chars < 0 or stats["before"] <= trigger_chars:
        return contexts, stats

    keep_recent = max(1, int(keep_recent or 12))
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
        limit = max(30, int(tool_chars if role == "tool" else old_chars))
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


def drop_oldest(
    contexts: list[Any],
    hard_limit: int,
    min_keep: int = 6,
) -> tuple[list[Any], int]:
    """硬上限兜底：还装不下就从最早的对话开始整块丢。

    `assistant(tool_calls)` 和紧随其后的 `tool` 结果是一个整体，要么整块留、
    要么整块删 —— 只删前一半会留下孤儿 tool 消息，接口直接 400。
    """
    out = list(contexts or [])
    dropped = 0
    guard = 0
    while context_chars(out) > hard_limit and len(out) > min_keep and guard < 500:
        guard += 1
        pick: tuple[int, int] | None = None
        for i in range(len(out)):
            msg = out[i]
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role in ("system", "_checkpoint", "tool"):
                continue
            n = 1
            if msg.get("tool_calls"):
                while (i + n < len(out) and isinstance(out[i + n], dict)
                       and out[i + n].get("role") == "tool"):
                    n += 1
            # 别把最近 min_keep 条切进这个块里
            if i + n > len(out) - min_keep:
                continue
            pick = (i, n)
            break
        if pick is None:
            break
        start, n = pick
        del out[start:start + n]
        dropped += n
    return out, dropped


def sanitize_contexts(
    contexts: list[Any],
    fix_image: bool = True,
    drop_think: bool = True,
    strip_images: bool = True,
    strip_music: bool = True,
    placeholder: str = "[之前发过的图片，你看过了，内容已省略]",
) -> tuple[list[Any], dict[str, int]]:
    """返回 (清理后的 contexts, 统计)。纯函数，方便离线自测。"""
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

                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    stats["unknown"] += 1
                    parts.append({"type": "text", "text": text})
                else:
                    stats["unknown"] += 1

            if not parts:
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
                if not (role == "tool" or msg.get("tool_calls")):
                    stats["empty"] += 1
                    continue
        elif content is None:
            if not (role == "tool" or msg.get("tool_calls")):
                stats["empty"] += 1
                continue

        out.append(msg)

    return out, stats


def _url_of(part: dict) -> Any:
    data = part.get("image_url") if "image_url" in part else part.get("image")
    if isinstance(data, dict):
        return data.get("url")
    return data


def _url_ok(url: Any) -> bool:
    return isinstance(url, str) and url.startswith(OK_PREFIX)


def hard_trim(contexts: list[Any], per_msg_chars: int) -> tuple[list[Any], int]:
    """最后一道：连"最近 N 条"也截。单条超长时（比如一次贴了几万字），
    keep_recent 的条数保护会让压缩完全不生效，只能按条截断。"""
    trimmed = 0
    out: list[Any] = []
    for msg in contexts or []:
        if not isinstance(msg, dict) or msg.get("role") in ("system", "_checkpoint"):
            out.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if len(content) > per_msg_chars:
                out.append({**msg, "content": _short_text(content, per_msg_chars)})
                trimmed += 1
            else:
                out.append(msg)
            continue
        if isinstance(content, list):
            parts: list[Any] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text") or "")
                    if len(text) > per_msg_chars:
                        parts.append({**part, "text": _short_text(text, per_msg_chars)})
                        trimmed += 1
                        continue
                parts.append(part)
            out.append({**msg, "content": parts})
            continue
        out.append(msg)
    return out, trimmed


def shrink_to_fit(
    contexts: list[Any],
    *,
    trigger_chars: int,
    keep_recent: int,
    old_chars: int,
    tool_chars: int,
    hard_limit: int,
    min_keep: int,
) -> tuple[list[Any], dict[str, int]]:
    """本地版核心：先按配置压一次，还超硬上限就一次比一次狠，最后整条丢。"""
    stats = {"before": context_chars(contexts), "after": 0,
             "compressed": 0, "dropped": 0, "trimmed": 0, "rounds": 0}
    cur = contexts
    keep, old, tool = keep_recent, old_chars, tool_chars
    trigger = trigger_chars
    if trigger <= 0:
        trigger = max(1, int(stats["before"] * 0.6))

    def over() -> bool:
        return hard_limit > 0 and context_chars(cur) > hard_limit

    # 第一轮：按配置正常压缩（最近 keep_recent 条不动）
    stats["rounds"] += 1
    cur, c = compress_contexts(cur, trigger_chars=trigger, keep_recent=keep,
                               old_chars=old, tool_chars=tool)
    stats["compressed"] += c["compressed"]

    # 第二步：超长单条先温和截断（保留开头，比整条压成一句体面得多）
    if over():
        for per_msg in (8000, 6000, 4000, 2000):
            cur, n = hard_trim(cur, per_msg)
            stats["trimmed"] += n
            stats["rounds"] += 1
            if not over():
                break

    # 第三步：还压不下来，最近的消息也不再保护了
    while over() and keep > 2:
        keep = max(2, keep // 2)
        old = max(40, old // 2)
        tool = max(60, tool // 2)
        stats["rounds"] += 1
        cur, c = compress_contexts(cur, trigger_chars=0, keep_recent=keep,
                                   old_chars=old, tool_chars=tool)
        stats["compressed"] += c["compressed"]

    # 第四步：整条丢弃（保底，宁可断片也不能 400）
    if over():
        cur, dropped = drop_oldest(cur, hard_limit, min_keep=max(2, min_keep))
        stats["dropped"] += dropped
        if dropped and isinstance(cur[0] if cur else None, dict):
            first = cur[0]
            if first.get("role") not in ("system", "_checkpoint"):
                head = {"role": "system", "content": DROPPED_MARK}
                cur = [head] + cur

    stats["after"] = context_chars(cur)
    return cur, stats


class ContextFixLocal(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config
        self._last_log = 0.0
        self._stripped_total = 0
        self._think_total = 0
        self._music_total = 0
        self._compress_total = 0
        self._drop_total = 0

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            if self.config is None:
                return default
            return self.config.get(key, default)
        except Exception:
            return default

    # 优先级最低 = 最后执行：别的插件（群聊增强、记忆召回）先改完 context，我们再收尾
    @filter.on_llm_request(priority=-1000000)
    async def clean_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not bool(self._cfg("enabled", DEFAULTS["enabled"])):
            return
        contexts = getattr(req, "contexts", None)
        if not contexts:
            return

        options = {
            "strip_images": bool(self._cfg("strip_images", DEFAULTS["strip_images"])),
            "fix_image": bool(self._cfg("fix_image", DEFAULTS["fix_image"])),
            "drop_think": bool(self._cfg("drop_think", DEFAULTS["drop_think"])),
            "strip_music": bool(self._cfg("strip_music", DEFAULTS["strip_music"])),
            "placeholder": str(self._cfg("placeholder", DEFAULTS["placeholder"])
                               or DEFAULTS["placeholder"]),
        }
        cleaned, stats = sanitize_contexts(contexts, **options)

        cstats = {"before": context_chars(cleaned), "after": context_chars(cleaned),
                  "compressed": 0, "dropped": 0, "trimmed": 0, "rounds": 0}
        if self._cfg("compress_enable", DEFAULTS["compress_enable"]):
            cleaned, cstats = shrink_to_fit(
                cleaned,
                trigger_chars=int(self._cfg("compress_trigger_chars",
                                            DEFAULTS["compress_trigger_chars"]) or 0),
                keep_recent=int(self._cfg("compress_keep_recent",
                                          DEFAULTS["compress_keep_recent"]) or 12),
                old_chars=int(self._cfg("compress_old_chars",
                                        DEFAULTS["compress_old_chars"]) or 90),
                tool_chars=int(self._cfg("compress_tool_chars",
                                         DEFAULTS["compress_tool_chars"]) or 120),
                hard_limit=int(self._cfg("hard_limit_chars",
                                         DEFAULTS["hard_limit_chars"]) or 0),
                min_keep=int(self._cfg("min_keep_messages",
                                       DEFAULTS["min_keep_messages"]) or 6),
            )

        if cleaned != contexts:
            req.contexts = cleaned  # type: ignore[assignment]

        touched = (stats["stripped"] + stats["image"] + stats["think"]
                   + stats["music"] + cstats["compressed"] + cstats["trimmed"]
                   + cstats["dropped"])
        if not touched:
            return

        self._stripped_total += stats["stripped"]
        self._think_total += stats["think"]
        self._music_total += stats["music"]
        self._compress_total += cstats["compressed"]
        self._drop_total += cstats["dropped"]
        now = time.time()
        every = float(self._cfg("log_every", DEFAULTS["log_every"]) or 60)
        if now - self._last_log >= every:
            self._last_log = now
            logger.info(
                f"{TAG} 本地瘦身：图片 {stats['stripped']} 张转文字、think {stats['think']} 个、"
                f"坏图 {stats['image']} 个、听歌笔记 {stats['music']} 条、"
                f"压缩 {cstats['compressed']} 条、单条截断 {cstats['trimmed']} 条、"
                f"丢 {cstats['dropped']} 条"
                f"（{cstats['before']} → {cstats['after']} 字，{cstats['rounds']} 轮）"
                f" → 剩 {len(cleaned)} 条消息"
                f"（累计省下 {self._stripped_total} 张图 / {self._think_total} 个推理块 / "
                f"{self._music_total} 条听歌笔记 / {self._compress_total} 条旧消息 / "
                f"{self._drop_total} 条整条丢弃）"
            )
