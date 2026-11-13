"""B 站写操作：点赞、投币、评论、动态。默认草稿 + 主人确认；点赞可低频率自动。"""

from __future__ import annotations

import random

from . import bili_api as api
from . import bili_state as st


def _ok(r: dict) -> tuple[bool, str]:
    code = int(r.get("code") if r.get("code") is not None else -1)
    msg = str(r.get("message") or r.get("msg") or "")
    return code == 0, f"code={code} {msg}".strip()


def _url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}/"


async def resolve_video(text: str, cred: dict | None) -> dict:
    bvid, aid = await api.resolve_target(text)
    if not bvid and not aid:
        raise RuntimeError("没认出 BV/av/b23 链接")
    return await api.view(bvid, aid, cred)


async def like(data: dict, cfg, cred: dict | None, auto: bool = False) -> str:
    if not cred:
        return "还没登录 B 站，先让主人发 /bili_login 扫码。"
    ok, why = st.check_quota("like", cfg)
    if not ok:
        return f"这次不点赞：{why}"
    r = await api.like(data, cred)
    good, detail = _ok(r)
    if good or "已经" in detail:
        st.record_quota("like")
        st.audit("like", data.get("bvid", ""), "", detail)
        return ("已点赞" if not auto else "顺手点了个赞") + f"（{data.get('title','')[:30]}）"
    st.audit("like", data.get("bvid", ""), "", detail, r.get("code", -1))
    return f"点赞没成功：{detail}"


async def unlike(data: dict, cred: dict | None) -> str:
    if not cred:
        return "还没登录 B 站，先登录再取消点赞。"
    r = await api.like(data, cred, unlike=True)
    good, detail = _ok(r)
    if good or "已经" in detail:
        st.audit("unlike", data.get("bvid", ""), "", detail)
        return f"已取消点赞（{data.get('title','')[:30]}）"
    st.audit("unlike", data.get("bvid", ""), "", detail, r.get("code", -1))
    return f"取消点赞没成功：{detail}"


async def coin_draft(data: dict, cfg, cred: dict | None, count: int = 1) -> str:
    did = st.add_draft("coin", {"bvid": data.get("bvid", ""), "title": data.get("title", ""),
                                "count": max(1, min(2, int(count)))})
    return (f"投币草稿 #{did}：《{data.get('title','')[:40]}》 {max(1, min(2, int(count)))} 个\n"
            f"确认发：/bili_ok {did}    取消：/bili_no {did}\n"
            "（硬币不能退，想清楚再确认）")


async def comment_draft(data: dict, cfg, cred: dict | None, message: str,
                        root: int | None = None, parent: int | None = None) -> str:
    message = (message or "").strip()
    if not message:
        return "评论内容是空的。"
    if len(message) > 1000:
        return "评论超过 1000 字，先删短一点。"
    did = st.add_draft("comment", {"bvid": data.get("bvid", ""), "aid": data.get("aid", 0),
                                   "title": data.get("title", ""), "message": message,
                                   "root": root, "parent": parent})
    return (f"评论草稿 #{did}（视频《{data.get('title','')[:40]}》）：\n{message}\n\n"
            f"确认发：/bili_ok {did}    取消：/bili_no {did}")


async def dynamic_draft(text: str, cfg, cred: dict | None, bvid: str = "",
                        pics: list[dict] | None = None) -> str:
    text = (text or "").strip()
    if not text and not pics:
        return "动态内容是空的。"
    if len(text) > 2000:
        return "动态超过 2000 字，先删短一点。"
    did = st.add_draft("dynamic", {"text": text, "bvid": bvid, "pics": pics or []})
    return (f"动态草稿 #{did}：\n{text}" + (f"\n附带视频：{_url(bvid)}" if bvid else "") +
            (f"\n附带图片：{len(pics)} 张" if pics else "") +
            f"\n\n确认发：/bili_ok {did}    取消：/bili_no {did}")


async def favorite(data: dict, cfg, cred: dict | None, folder: str = "",
                   cancel: bool = False) -> str:
    if not cred:
        return "还没登录 B 站，先扫码。"
    ok, why = st.check_quota("favorite", cfg)
    if not ok:
        return f"这次不收藏：{why}"
    r, f = await api.fav_deal(data, cred, folder, cancel)
    good, detail = _ok(r)
    if good or "已经" in detail:
        st.record_quota("favorite")
        st.audit("unfavorite" if cancel else "favorite", data.get("bvid", ""),
                 f.get("title", ""), detail)
        return (f"已取消收藏" if cancel else f"已收藏到「{f.get('title')}」") + \
               f"（{data.get('title','')[:30]}）"
    st.audit("favorite", data.get("bvid", ""), "", detail, r.get("code", -1))
    return f"收藏没成功：{detail}"


async def follow(mid: str, cfg, cred: dict | None, cancel: bool = False) -> str:
    if not cred:
        return "还没登录 B 站，先扫码。"
    ok, why = st.check_quota("follow", cfg)
    if not ok:
        return f"这次不关注：{why}"
    r = await api.follow(mid, cred, cancel)
    good, detail = _ok(r)
    if good or "已经" in detail:
        st.record_quota("follow")
        st.audit("unfollow" if cancel else "follow", str(mid), "", detail)
        return f"已{'取消关注' if cancel else '关注'} UID {mid}"
    st.audit("follow", str(mid), "", detail, r.get("code", -1))
    return f"关注没成功：{detail}"


async def watch_later(data: dict, cfg, cred: dict | None, cancel: bool = False) -> str:
    if not cred:
        return "还没登录 B 站，先扫码。"
    ok, why = st.check_quota("watch_later", cfg)
    if not ok:
        return f"这次不加稍后再看：{why}"
    r = await api.watch_later_set(data, cred, cancel)
    good, detail = _ok(r)
    if good or "已经" in detail:
        st.record_quota("watch_later")
        st.audit("watch_later", data.get("bvid", ""), "", detail)
        return f"已{'移出' if cancel else '加入'}稍后再看（{data.get('title','')[:30]}）"
    st.audit("watch_later", data.get("bvid", ""), "", detail, r.get("code", -1))
    return f"稍后再看没设置成功：{detail}"


async def triple(data: dict, cfg, cred: dict | None, folder: str = "") -> str:
    if not cred:
        return "还没登录 B 站，先扫码。"
    ok, why = st.check_quota("triple", cfg)
    if not ok:
        return f"这次不三连：{why}"
    parts = []
    try:
        r1 = await api.like(data, cred)
        parts.append("点赞" + ("成功" if _ok(r1)[0] else f"失败({_ok(r1)[1]})"))
    except Exception as e:
        parts.append(f"点赞出错({e})")
    try:
        r2 = await api.coin(data, cred, 1)
        parts.append("投币" + ("成功" if _ok(r2)[0] else f"失败({_ok(r2)[1]})"))
    except Exception as e:
        parts.append(f"投币出错({e})")
    try:
        r3, f = await api.fav_deal(data, cred, folder, False)
        parts.append("收藏" + ("成功" if _ok(r3)[0] else f"失败({_ok(r3)[1]})"))
    except Exception as e:
        parts.append(f"收藏出错({e})")
    st.record_quota("triple")
    text = "；".join(parts)
    st.audit("triple", data.get("bvid", ""), text, "ok")
    return f"三连结果：{text}"


async def dm_draft(to_uid: str, text: str, cfg, cred: dict | None) -> str:
    to_uid = str(to_uid or "").strip()
    text = (text or "").strip()
    if not to_uid or not text:
        return "私信需要收件人 UID 和内容。"
    if len(text) > 500:
        return "私信超过 500 字，先删短一点。"
    did = st.add_draft("dm", {"to": to_uid, "text": text})
    return (f"私信草稿 #{did} → UID {to_uid}：\n{text}\n\n"
            f"确认发：/bili_ok {did}    取消：/bili_no {did}")


async def danmaku_draft(room_id: str, text: str, cfg, cred: dict | None) -> str:
    room_id = str(room_id or "").strip()
    text = (text or "").strip()
    if not room_id or not text:
        return "发弹幕需要直播间号和内容。"
    if len(text) > 100:
        return "弹幕超过 100 字，先删短一点。"
    did = st.add_draft("danmaku", {"room": room_id, "text": text})
    return (f"弹幕草稿 #{did}（房间 {room_id}）：\n{text}\n\n"
            f"确认发：/bili_ok {did}    取消：/bili_no {did}")


async def share_draft(data: dict, cfg, cred: dict | None, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "分享内容为空。"
    did = st.add_draft("share", {"bvid": data.get("bvid", ""), "title": data.get("title", ""),
                                 "up_mid": str((data.get("owner") or {}).get("mid") or ""),
                                 "text": text})
    return (f"转发/分享草稿 #{did}：《{data.get('title','')[:40]}》\n{text}\n"
            f"{_url(data.get('bvid',''))}\n\n确认发：/bili_ok {did}    取消：/bili_no {did}")


async def execute_draft(did: str, cfg, cred: dict | None) -> str:
    item = st.get_draft(did)
    if not item:
        return f"没有草稿 #{did}"
    if not cred:
        return "还没登录 B 站，先发 /bili_login 扫码。"
    kind = item.get("kind")
    try:
        if kind == "coin":
            ok, why = st.check_quota("coin", cfg)
            if not ok:
                return f"这次不投币：{why}"
            data = await resolve_video(item.get("bvid", ""), cred)
            r = await api.coin(data, cred, int(item.get("count") or 1))
            good, detail = _ok(r)
            if good:
                st.record_quota("coin")
                st.audit("coin", item.get("bvid", ""), "", detail)
                st.drop_draft(did)
                return f"已投币 {item.get('count')} 个（{detail}）"
            return f"投币没成功：{detail}"
        if kind == "comment":
            ok, why = st.check_quota("comment", cfg)
            if not ok:
                return f"这次不评论：{why}"
            data = {"bvid": item.get("bvid"), "aid": item.get("aid")}
            r = await api.comment_add(data, cred, item.get("message", ""),
                                      item.get("root"), item.get("parent"))
            good, detail = _ok(r)
            if good:
                st.record_quota("comment")
                st.audit("comment", item.get("bvid", ""), item.get("message", ""), detail)
                st.drop_draft(did)
                return "评论已发（已记账）"
            return f"评论没成功：{detail}"
        if kind in ("dynamic", "share"):
            action = "dynamic" if kind == "dynamic" else "share"
            ok, why = st.check_quota(action, cfg)
            if not ok:
                return f"这次不发动态：{why}"
            text = item.get("text", "")
            if kind == "share" and item.get("bvid"):
                card = None
                try:
                    card = await api.find_video_dynamic(item.get("bvid", ""),
                                                        item.get("up_mid", ""), cred)
                except Exception:
                    card = None
                if card and card.get("id"):
                    r = await api.forward_dynamic(card["id"], text, cred)
                else:
                    r = await api.dynamic_post(f"{text}\n{_url(item['bvid'])}", cred)
            else:
                r = await api.dynamic_post(text, cred, item.get("pics") or [])
            good, detail = _ok(r)
            if good:
                st.record_quota(action)
                st.audit(action, item.get("bvid", ""), text, detail)
                st.drop_draft(did)
                dyn_id = ((r.get("data") or {}).get("dyn_id_str")
                          or (r.get("data") or {}).get("dyn_id")
                          or (r.get("data") or {}).get("dynamic_id_str")
                          or (r.get("data") or {}).get("dynamic_id") or "")
                return f"动态已发{f'（{dyn_id}）' if dyn_id else ''}"
            return f"动态没发出去：{detail}"
        if kind == "dm":
            ok, why = st.check_quota("dm", cfg)
            if not ok:
                return f"这次不发私信：{why}"
            r = await api.dm_send(str(item.get("to") or ""), item.get("text", ""), cred)
            good, detail = _ok(r)
            if good:
                st.record_quota("dm")
                st.audit("dm", str(item.get("to") or ""), item.get("text", ""), detail)
                st.drop_draft(did)
                return f"私信已发（UID {item.get('to')}）"
            return f"私信没发出去：{detail}"
        if kind == "danmaku":
            ok, why = st.check_quota("danmaku", cfg)
            if not ok:
                return f"这次不发弹幕：{why}"
            r = await api.live_send_danmaku(str(item.get("room") or ""),
                                            item.get("text", ""), cred)
            good, detail = _ok(r)
            if good:
                st.record_quota("danmaku")
                st.audit("danmaku", str(item.get("room") or ""), item.get("text", ""), detail)
                st.drop_draft(did)
                return f"弹幕已发（房间 {item.get('room')}）"
            return f"弹幕没发出去：{detail}"
        return f"未知草稿类型：{kind}"
    except Exception as e:
        return f"执行草稿出错：{e}"


async def maybe_auto_like(data: dict, cfg, cred: dict | None) -> str:
    try:
        if not _cfg(cfg, "auto_like", False) or not cred:
            return ""
        if random.random() > float(_cfg(cfg, "auto_like_probability", 0.5) or 0.5):
            return ""
        return await like(data, cfg, cred, auto=True)
    except Exception:
        return ""


def _cfg(cfg, key, default=None):
    try:
        v = cfg.get(key, default) if hasattr(cfg, "get") else None
        return default if v is None else v
    except Exception:
        return default


def list_drafts() -> list[dict]:
    d = st._load(st.DRAFTS_FILE, {})
    if not isinstance(d, dict):
        return []
    return [v for k, v in d.items() if not str(k).startswith("_") and isinstance(v, dict)]
