"""大肥鱼·离线补回复

服务（AstrBot）启动时，回头看一眼：有没有你发过来、但她根本没回的消息。
有就当场补上——像她刚忙完拿起手机那样自然，而不是"系统提示：您有一条未读"。

判定依据是会话历史本身（`conversations` 表的 history）：
  · 最后一条 assistant 之后的 user 消息 = 没被回复的
  · 另外可选地找 SnowLuma/OneBot 拉最近私聊记录，捞出"那会儿她根本没在线、
    消息压根没进 AstrBot"的那种（靠文本去重，不会重复回）

补完之后会把回复写回历史，所以重启不会重复补。

手动触发：私聊发 `/补回复`
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

DEFAULTS = {
    "owner_qq": "10000000",
    "enabled": True,
    "delay_seconds": 35,
    "max_age_hours": 72,
    "max_replies": 3,
    "max_lines": 3,
    "fetch_from_onebot": True,
    "onebot_count": 20,
    "temperature": 0.95,
    "max_context_msgs": 30,
    "user_msg_max_chars": 2000,
    "merge_lines": True,
    "min_gap_minutes": 30,
    "dry_run": False,
}

# 提示词的重点不是"教她说话"（人设已经写得很细），而是：
#   ① 让她明白这是"他刚才连着发的几句"，别当成一句；
#   ② 把最容易滑向客服腔的几种写法直接点名禁掉；
#   ③ 给两个反例/正例，模型抄感觉比抄规则准。
INSTRUCTION = """
【补一句遗漏的回复】
下面是他刚才发给你的原话，按时间先后（他可能连着发了好几条）。你刚才没顾上回，现在拿起手机看见了。

怎么回：
- 这条是**文字补发**：不要提语音、不要写「语音已发送」，也不要假装已经用语音说过
- 挑他话里**最值得接的那一个点**放大，别逐条对应——一条一条回到像在回工单
- 可以是：接住那个点、吐槽他、追问一句、说你自己刚在干嘛、或者只对他某个词有反应
- 这些开场别用：「刚在忙」「怎么了」「在的」「我看看」「好的」「收到」「有什么可以帮你的」
- 别用"正确但没信息量"的安慰：辛苦啦、加油、别太累、抱抱你。要说就说具体的
  （比如「会开这么久，你们领导话真多」），说不出来就别说安慰
- 别解释自己为什么没回，除非他问；别连着道歉
- 不总结、不列点、不写道理、不排比
- 最多 {max_lines} 行，能一句说完就别凑三句；只回一句甚至一个「?」也可以
- 直接输出要发出去的话：不要引号、不要括号里的旁白

反例（不要这样，空泛像客服）：
> 他说「今天面试过了」
> 你：「恭喜你呀，太棒了，我就知道你一定可以的！」

正例：
> 「真的假的」
> 「我就说你行嘛」
> 「请我吃饭，两碗token起步」

反例（逐条对应，工单感）：
> 他说「刚开完会头都大了 / 书下单了 / 晚上吃什么」
> 你：「刚开完会辛苦啦，书这么快就下单了，晚上想吃啥口味的我给你挑」

正例（只挑一个点放大，剩下的顺嘴带过）：
> 「开完会就这下场」
> 「书到了先给我看一眼封面」
> 「晚上火锅，我要辣的」
"""


def _strip_reminder(text: str) -> str:
    """去掉 AstrBot 塞在用户消息里的 <system_reminder>…</system_reminder>。"""
    if not isinstance(text, str):
        return ""
    out = []
    skip = False
    for line in text.splitlines():
        low = line.strip()
        if low.startswith("<system_reminder>"):
            skip = True
        if not skip:
            out.append(line)
        if "</system_reminder>" in low:
            skip = False
    return "\n".join(out).strip()


def msg_text(msg: dict) -> str:
    """把一条历史消息压成纯文本（兼容 str 和 list-of-parts 两种存法）。

    目标是「她看到的就是他说的话」：正文原样保留（含换行），
    system_reminder / 图片本地路径这些噪音去掉，只留一个 [图片] 提示。
    """
    content = msg.get("content")
    if isinstance(content, str):
        return _strip_reminder(content)
    if isinstance(content, list):
        chunks = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                t = _strip_reminder(part.get("text") or "")
                if not t:
                    continue
                # AstrBot 会插一条「[Image 1: original path E:\...]」的说明文本，别让她读到路径
                if t.startswith("[Image ") and "original path" in t:
                    continue
                chunks.append(t)
            elif kind in ("image_url", "image"):
                chunks.append("[图片]")
            elif kind == "record":
                chunks.append("[语音]")
        # 相邻的 [图片] 去重（说明文本 + image_url 会各来一次）
        out = []
        for c in chunks:
            if out and c == out[-1] == "[图片]":
                continue
            out.append(c)
        return "\n".join(out).strip()
    return ""


def normalize(text: str, n: int = 40) -> str:
    return "".join(str(text or "").split())[:n]


def strip_voice_artifacts(text: str) -> str:
    """去掉语音工具留下的旁白，补回复这条链路只发文字。

    历史里她以前的语音回复会留下「（语音已发送～…）」这类记录，
    模型会照着学，导致补回复的文字里也带一句「语音已发送」，用户以为有语音。
    """
    if not text:
        return ""
    # 整段括号旁白（含"语音已发送"/"已用语音"）
    text = re.sub(r"[（(][^（）()]*(?:语音已发送|已用语音)[^（）()]*[）)]", "", text)
    # 行首/行中残余的「语音已发送～」
    text = re.sub(r"[，,～~：:\s]*语音已发送\s*[～~，,：:。！!]?\s*", "", text)
    return text.strip()


class DafeiyuBackfill(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        cfg = dict(DEFAULTS)
        if isinstance(config, dict):
            for k in DEFAULTS:
                if config.get(k) is not None:
                    cfg[k] = config[k]
        self.cfg = cfg
        self._dir = os.path.join(
            get_astrbot_plugin_data_path(), "astrbot_plugin_dafeiyu_backfill"
        )
        os.makedirs(self._dir, exist_ok=True)
        self._log_path = os.path.join(self._dir, "backfill.log")
        self._state_path = os.path.join(self._dir, "state.json")
        self._onebot_off = False
        self._task = None
        try:
            self._task = asyncio.create_task(self._boot())
        except RuntimeError:
            pass

    # ---------- 基础设施 ----------

    def note(self, text: str) -> None:
        line = f"[{datetime.now():%m-%d %H:%M:%S}] {text}"
        logger.info(f"[dafeiyu-backfill] {text}")
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    async def _boot(self) -> None:
        if not self.cfg["enabled"]:
            self.note("未启用，跳过")
            return
        await asyncio.sleep(max(5, int(self.cfg["delay_seconds"])))
        try:
            gap = float(self.cfg.get("min_gap_minutes") or 0) * 60
            last = float(self._load_state().get("last_run") or 0)
            if gap and time.time() - last < gap:
                self.note(f"距上次检查不到 {gap/60:g} 分钟，本轮跳过（防止反复重启刷消息）")
                return
            if not await self._wait_platform_ready(90):
                self.note("协议端还没连上，先不补（下次启动或 /补回复 再试）")
                return
            await self.run_once("服务启动")
        except Exception as e:
            logger.error(f"[dafeiyu-backfill] 启动补回复出错: {e}")

    # ---------- 状态（防止反复重启重复补） ----------

    def _load_state(self) -> dict:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self, **kw) -> None:
        st = self._load_state()
        st.update(kw)
        try:
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_path)
        except Exception as e:
            logger.warning(f"[dafeiyu-backfill] 状态保存失败: {e}")

    async def _wait_platform_ready(self, timeout: int) -> bool:
        """等 aiocqhttp 适配器真正连上（AstrBot 刚起来时它可能是 None）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                for conv in await self.context.conversation_manager.get_conversations():
                    umo = str(getattr(conv, "user_id", "") or "")
                    if "FriendMessage" in umo and umo.endswith(str(self.cfg["owner_qq"])):
                        pid = MessageSession.from_str(umo).platform_id
                        if self.context.get_platform_inst(pid):
                            return True
            except Exception:
                pass
            await asyncio.sleep(8)
        return False

    # ---------- 找会话 ----------

    async def _owner_sessions(self) -> list[dict]:
        """所有「本人私聊」的会话（umo + cid + history）。"""
        owner = str(self.cfg["owner_qq"])
        cm = self.context.conversation_manager
        out = []
        try:
            convs = await cm.get_conversations()
        except Exception as e:
            logger.warning(f"[dafeiyu-backfill] 读会话列表失败: {e}")
            return out
        for conv in convs:
            umo = str(getattr(conv, "user_id", "") or "")
            if not umo or "FriendMessage" not in umo or not umo.endswith(owner):
                continue
            # 补回复只走 OneBot（SnowLuma/NapCat）；Telegram 等其他平台没有
            # get_friend_msg_history，也没有「协议端丢消息」这回事。
            try:
                if MessageSession.from_str(umo).platform_name != "aiocqhttp":
                    continue
            except Exception:  # noqa: BLE001
                continue
            history = []
            raw = getattr(conv, "history", None)
            if isinstance(raw, str) and raw:
                try:
                    history = json.loads(raw)
                except Exception:
                    history = []
            elif isinstance(raw, list):
                history = raw
            out.append(
                {
                    "umo": umo,
                    "cid": getattr(conv, "conversation_id", None),
                    "history": history if isinstance(history, list) else [],
                }
            )
        return out

    # ---------- 找没回的消息 ----------

    @staticmethod
    def _pending_from_history(history: list) -> list[str]:
        """最后一条 assistant 之后的 user 文本（没被回的）。"""
        last_assistant = -1
        for i, m in enumerate(history):
            if isinstance(m, dict) and m.get("role") == "assistant":
                last_assistant = i
        tail = history[last_assistant + 1:]
        texts = []
        for m in tail:
            if isinstance(m, dict) and m.get("role") == "user":
                t = msg_text(m)
                if not t:
                    continue
                # 手动命令本身也会进历史，别把它当成"没回的消息"
                if t.lstrip().startswith("/补回复"):
                    continue
                texts.append(t)
        return texts

    async def _friend_history(self, bot) -> dict:
        """拉最近私聊记录。

        NapCat / SnowLuma 对 `message_seq` 的语义不一样：传 0 会报
        `private roam history cursor did not advance`，所以先不带这个参数试，
        不行再退回带 0 的老写法。
        """
        uid = int(self.cfg["owner_qq"])
        count = int(self.cfg["onebot_count"])
        attempts = [
            {"user_id": uid, "count": count},
            {"user_id": uid, "message_seq": 0, "count": count},
        ]
        last_err: Exception | None = None
        for kw in attempts:
            try:
                return await bot.call_action("get_friend_msg_history", **kw)
            except Exception as e:
                last_err = e
        raise last_err or RuntimeError("get_friend_msg_history 调用失败")

    async def _onebot_missing(self, umo: str, history: list) -> list[str]:
        """从协议端拉最近私聊，捞出根本没进 AstrBot 的消息（按文本去重）。"""
        if not self.cfg["fetch_from_onebot"] or self._onebot_off:
            return []
        platform_id = MessageSession.from_str(umo).platform_id
        adapter = self.context.get_platform_inst(platform_id)
        bot = getattr(adapter, "bot", None) if adapter else None
        if bot is None:
            return []
        try:
            res = await self._friend_history(bot)
        except Exception as e:
            # SnowLuma 的 get_friend_msg_history 要有效的游标，拿不到就这一轮先放弃，
            # 本进程内不再重试（免得每个会话都白跑两次，还刷日志）
            self._onebot_off = True
            self.note(f"拉协议端历史失败（本轮跳过，靠本地历史补）: {e}")
            return []
        messages = []
        if isinstance(res, dict):
            messages = res.get("messages") or res.get("data", {}).get("messages") or []
        if not isinstance(messages, list):
            return []

        known = set()
        for m in history[-40:]:
            if isinstance(m, dict) and m.get("role") == "user":
                t = msg_text(m)
                if t:
                    known.add(normalize(t))

        cutoff = time.time() - float(self.cfg["max_age_hours"]) * 3600
        missing = []
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            sender = raw.get("sender") or {}
            if str(sender.get("user_id") or "") != str(self.cfg["owner_qq"]):
                continue
            if float(raw.get("time") or 0) < cutoff:
                continue
            text = raw.get("raw_message") or ""
            if not text:
                for part in raw.get("message") or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text += str(part.get("data", {}).get("text") or "")
            text = str(text).strip()
            if not text or text.startswith("/补回复") or normalize(text) in known:
                continue
            missing.append(text)
        return missing[-int(self.cfg["max_replies"]):]

    # ---------- 生成与发送 ----------

    async def _generate(self, umo: str, contexts: list, pending: list[str]) -> str:
        provider = await self.context.get_using_provider_async(umo)
        if not provider:
            self.note("没有可用的对话模型")
            return ""
        persona = self._persona_prompt(umo)
        instruction = INSTRUCTION.format(max_lines=int(self.cfg["max_lines"]))
        system_prompt = (persona + "\n\n" + instruction) if persona else instruction
        prompt = "\n".join(pending)
        temp = float(self.cfg.get("temperature") or 0.0)
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt,
                temperature=temp,
            )
        except Exception as e:
            # 有的兼容接口不认 temperature，退回默认采样，别让补回复直接失败
            self.note(f"带 temperature={temp} 调用失败（{e}），改用默认参数重试")
            try:
                resp = await provider.text_chat(
                    prompt=prompt, contexts=contexts, system_prompt=system_prompt
                )
            except Exception as e2:
                self.note(f"生成失败: {e2}")
                return ""
        raw = (getattr(resp, "completion_text", "") or "").strip()
        cleaned = self._clean(raw)
        if not cleaned:
            # 关键排障信息：模型到底回了什么、还是根本没回
            self.note(
                f"模型返回空内容（raw={raw!r}, resp={type(resp).__name__}, "
                f"contexts={len(contexts)}）"
            )
        return cleaned

    def _clean(self, raw: str) -> str:
        # 人设让她在结尾带 &&情绪&&（主动插件拿它选表情包）；这条链路不选表情包，
        # 不剥掉的话就会原样发到 QQ 上。
        raw = re.sub(r"&&[A-Za-z_\u4e00-\u9fa5]{1,20}&&", "", raw or "")
        lines = []
        for line in raw.splitlines():
            t = line.strip().strip("\"'“”‘’")
            if t:
                lines.append(t)
        if not lines:
            return ""

        def narration(s: str) -> bool:
            return (s.startswith("（") and s.endswith("）")) or (
                s.startswith("(") and s.endswith(")")
            )

        # 多行时丢掉纯旁白行；但如果整条回复就是括号包住的（模型常这么干），
        # 不能整条扔掉——去掉外层括号，内容照发。
        kept = [t for t in lines if not (narration(t) and len(lines) > 1)] or lines
        out = []
        for t in kept:
            if narration(t):
                t = t[1:-1].strip()
            if t:
                out.append(t)
            if len(out) >= int(self.cfg["max_lines"]):
                break
        text = "\n".join(out)
        # 模型有时会自己补一句"语音已发送"，这是工具旁白，不是要说的话
        text = strip_voice_artifacts(text)
        return text

    async def _send(self, umo: str, text: str) -> bool:
        try:
            session = MessageSession.from_str(umo)
            platform = self.context.get_platform_inst(
                session.platform_id
            ) or self.context.get_platform(session.platform_name)
            if not platform:
                self.note("找不到平台适配器（协议端没连？）")
                return False
            # 并成一条发：分条连发是 QQ 风控最敏感的行为
            if self.cfg.get("merge_lines", True):
                await platform.send_by_session(session, MessageChain([Plain(text)]))
            else:
                for line in text.splitlines():
                    await platform.send_by_session(session, MessageChain([Plain(line)]))
                    await asyncio.sleep(4)
            self._save_state(last_send=time.time())
            return True
        except Exception as e:
            self.note(f"发送失败: {e}")
            return False

    def _persona_prompt(self, umo: str) -> str:
        try:
            cfg = self.context.get_config(umo=umo) or {}
            conf = (cfg.get("agent_runner") or {}).get("config") or {}
            pid = (conf.get("persona") or {}).get("persona_id")
            pm = getattr(self.context, "persona_manager", None)
            if pid and pm:
                p = pm.get_persona_v3_by_id(pid)
                if p and getattr(p, "system_prompt", ""):
                    return p.system_prompt
        except Exception as e:
            logger.warning(f"[dafeiyu-backfill] 取人设失败: {e}")
        return ""

    def _to_contexts(self, history: list, keep: int | None = None) -> list[dict]:
        """把历史转成模型能吃的 contexts。

        关键：**他说的话要留全**（默认 2000 字），以前一刀切 400 字会把长消息砍半，
        她只能看到半句，回出来自然驴唇不对马嘴。她自己的回复留 300 字就够。
        """
        keep = int(keep or self.cfg.get("max_context_msgs") or 30)
        user_cap = int(self.cfg.get("user_msg_max_chars") or 2000)
        out = []
        for m in history[-keep:]:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            t = msg_text(m)
            if not t:
                continue
            t = strip_voice_artifacts(t)
            if not t:
                continue
            cap = user_cap if role == "user" else 300
            out.append({"role": role, "content": t[:cap]})
        return out

    # ---------- 主流程 ----------

    async def run_once(self, reason: str = "手动") -> dict:
        self._save_state(last_run=time.time())
        sessions = await self._owner_sessions()
        if not sessions:
            self.note(f"[{reason}] 没找到本人私聊会话")
            return {"replied": 0, "pending": 0}

        replied = 0
        pending_seen = 0
        for s in sessions:
            if replied >= int(self.cfg["max_replies"]):
                break
            umo, cid, history = s["umo"], s["cid"], s["history"]
            pending = self._pending_from_history(history)
            extra = await self._onebot_missing(umo, history)
            for e in extra:
                if normalize(e) not in {normalize(p) for p in pending}:
                    pending.append(e)
            if not pending:
                continue
            pending_seen += len(pending)

            # 待回复的那几条不进 contexts，避免和 prompt 重复。
            # 用「最后一条 assistant」切，而不是按 pending 条数倒推：
            # OneBot 补来的消息不在 history 里，按条数减会把正常历史也砍掉。
            last_assistant = -1
            for i, m in enumerate(history):
                if isinstance(m, dict) and m.get("role") == "assistant":
                    last_assistant = i
            ctx_history = history[: last_assistant + 1]
            contexts = self._to_contexts(ctx_history)
            self.note(f"[{reason}] {umo} 待补 {len(pending)} 条: {pending!r}")
            self.note(f"[{reason}] 调用模型生成（上下文 {len(contexts)} 条）…")

            reply = await self._generate(umo, contexts, pending)
            if not reply:
                continue
            self.note(f"[{reason}] 生成回复: {reply!r}")

            if self.cfg["dry_run"]:
                self.note("[dry_run] 只记录不发送")
                replied += 1
                continue

            if not await self._send(umo, reply):
                continue

            # 把回复（以及协议端补来的用户消息）写回历史，重启后不会再补
            new_history = list(history)
            known = {normalize(p) for p in self._pending_from_history(history)}
            for text in pending:
                if normalize(text) not in known:
                    new_history.append(
                        {"role": "user", "content": [{"type": "text", "text": text}]}
                    )
            new_history.append(
                {"role": "assistant", "content": [{"type": "text", "text": reply}]}
            )
            try:
                await self.context.conversation_manager.update_conversation(
                    umo, cid, history=new_history
                )
            except Exception as e:
                self.note(f"写回历史失败（下次可能会重复补）: {e}")

            replied += 1
            await asyncio.sleep(2)

        if not replied:
            if pending_seen:
                self.note(
                    f"[{reason}] 检测到 {pending_seen} 条待补，但这次没生成出可发送的内容"
                )
            else:
                self.note(f"[{reason}] 没有需要补回复的消息")
        return {"replied": replied, "pending": pending_seen}

    # ---------- 手动入口 ----------

    @filter.command("补回复")
    async def manual(self, event: AstrMessageEvent):
        """手动检查一次未回复的消息（私聊发 /补回复）"""
        if str(event.get_sender_id() or "") != str(self.cfg["owner_qq"]):
            return
        yield event.plain_result("我去翻翻有没有漏回的…")
        try:
            res = await self.run_once("手动")
        except Exception as e:
            logger.error(f"[dafeiyu-backfill] 手动补回复出错: {e}")
            yield event.plain_result(f"出错了：{e}")
            return
        if res.get("replied"):
            yield event.plain_result("补好了")
        elif res.get("pending"):
            yield event.plain_result(
                f"翻到 {res['pending']} 条没回的，但这次没生成出来（看日志）"
            )
        else:
            yield event.plain_result("没有漏的，都回过了")
