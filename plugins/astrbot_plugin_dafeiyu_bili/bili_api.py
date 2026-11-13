"""B 站 HTTP 客户端：WBI、视频/字幕/弹幕/评论、扫码、点赞/投币/评论/动态。"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import time
from urllib.parse import quote

import aiohttp

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
REF = "https://www.bilibili.com/"
API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
# 至少两位数字、且前后不能贴着字母数字：避免把 AV1 编码、avatar123 之类当 av 号
AV_RE = re.compile(r"(?<![0-9A-Za-z])av(\d{2,12})(?![0-9A-Za-z])", re.I)
B23_RE = re.compile(r"https?://b23\.tv/[A-Za-z0-9]+")

MIXIN_TAB = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,
             29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
             22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

_wbi = {"mixin": "", "t": 0.0}


def cookie_header(cred: dict | None) -> str:
    if not cred:
        return ""
    pairs = []
    if cred.get("sessdata"):
        pairs.append(f"SESSDATA={cred['sessdata']}")
    if cred.get("bili_jct"):
        pairs.append(f"bili_jct={cred['bili_jct']}")
    if cred.get("buvid3"):
        pairs.append(f"buvid3={cred['buvid3']}")
    return "; ".join(pairs)


def headers(cred: dict | None = None, referer: str = REF, json_body: bool = False) -> dict:
    h = {"user-agent": UA, "referer": referer, "accept": "application/json, text/plain, */*"}
    ck = cookie_header(cred)
    if ck:
        h["cookie"] = ck
    if json_body:
        h["content-type"] = "application/json"
    return h


async def get_json(url: str, cred: dict | None = None, referer: str = REF,
                   timeout: float = 20.0) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers(cred, referer),
                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            text = await r.text()
            try:
                return json.loads(text)
            except Exception:
                raise RuntimeError(f"HTTP {r.status}: {text[:200]}")


async def post_form(url: str, data: dict, cred: dict | None = None,
                    referer: str = REF, timeout: float = 30.0) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=data, headers=headers(cred, referer),
                          timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            text = await r.text()
            try:
                return json.loads(text)
            except Exception:
                raise RuntimeError(f"HTTP {r.status}: {text[:200]}")


async def post_json(url: str, payload: dict, cred: dict | None = None,
                    referer: str = REF, origin: str = "https://t.bilibili.com",
                    timeout: float = 30.0) -> dict:
    h = headers(cred, referer, json_body=True)
    h["origin"] = origin
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=h,
                          timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            text = await r.text()
            try:
                return json.loads(text)
            except Exception:
                raise RuntimeError(f"HTTP {r.status}: {text[:200]}")


async def post_multipart(url: str, fields: dict, cred: dict | None = None,
                         referer: str = "https://t.bilibili.com/",
                         timeout: float = 30.0) -> dict:
    form = aiohttp.FormData()
    for k, v in fields.items():
        form.add_field(str(k), str(v))
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=form, headers=headers(cred, referer),
                          timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            text = await r.text()
            try:
                return json.loads(text)
            except Exception:
                raise RuntimeError(f"HTTP {r.status}: {text[:200]}")


async def nav(cred: dict | None = None) -> dict:
    d = await get_json(f"{API}/x/web-interface/nav", cred)
    return d.get("data") or {}


async def wbi_mixin(cred: dict | None = None) -> str:
    now = time.time()
    if _wbi["mixin"] and now - _wbi["t"] < 1800:
        return _wbi["mixin"]
    d = await get_json(f"{API}/x/web-interface/nav", cred)
    img = ((d.get("data") or {}).get("wbi_img") or {})
    raw = ""
    for k in ("img_url", "sub_url"):
        u = str(img.get(k) or "")
        if u:
            raw += u[u.rfind("/") + 1:u.rfind(".")]
    if len(raw) < 64:
        return ""
    mixin = "".join(raw[i] for i in MIXIN_TAB if i < len(raw))[:32]
    _wbi.update({"mixin": mixin, "t": now})
    return mixin


def wbi_query(params: dict, mixin: str) -> str:
    wts = int(time.time())
    p = dict(params)
    p["wts"] = wts
    items = []
    for k in sorted(p):
        v = str(p[k]).replace("!", "").replace("'", "").replace("(", "").replace(")", "").replace("*", "")
        items.append(f"{quote(str(k), safe='')}={quote(v, safe='')}")
    q = "&".join(items)
    if mixin:
        q += "&w_rid=" + hashlib.md5((q + mixin).encode()).hexdigest()
    return q


async def resolve_target(text: str) -> tuple[str | None, int | None]:
    """从文本/短链里认 BV 或 av。返回 (bvid, aid)。"""
    text = text or ""
    m = BV_RE.search(text)
    if m:
        return m.group(0), None
    m = B23_RE.search(text)
    if m:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(m.group(0), headers={"user-agent": UA},
                                 allow_redirects=True,
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    final = str(r.url)
            m2 = BV_RE.search(final)
            if m2:
                return m2.group(0), None
            m2 = AV_RE.search(final)
            if m2:
                return None, int(m2.group(1))
        except Exception:
            pass
    m = AV_RE.search(text)
    if m:
        return None, int(m.group(1))
    return None, None


async def view(bvid: str | None, aid: int | None = None, cred: dict | None = None) -> dict:
    if bvid:
        d = await get_json(f"{API}/x/web-interface/view?bvid={bvid}", cred)
    elif aid:
        d = await get_json(f"{API}/x/web-interface/view?aid={aid}", cred)
    else:
        raise RuntimeError("没有 BV/aid")
    if d.get("code") != 0:
        raise RuntimeError(f"view 失败 code={d.get('code')} {d.get('message')}")
    return d.get("data") or {}


async def subtitles(data: dict, cred: dict | None) -> tuple[list[dict], str, str]:
    """返回 (字幕行, 轨道, 来源)。字幕行：[{from,to,content}]。"""
    bvid = data.get("bvid") or ""
    aid = data.get("aid") or 0
    cid = data.get("cid") or (data.get("pages") or [{}])[0].get("cid") or 0
    if not cid:
        return [], "", ""
    mixin = await wbi_mixin(cred)
    subs = []
    src = ""
    if mixin:
        q = wbi_query({"bvid": bvid, "cid": cid}, mixin)
        try:
            p = await get_json(f"{API}/x/player/wbi/v2?{q}", cred,
                               referer=f"https://www.bilibili.com/video/{bvid}/")
            subs = ((p.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            src = "wbi/v2"
        except Exception:
            subs = []
    if not subs:
        try:
            ext = quote(json.dumps({"video_type": 1}), safe="")
            p2 = await get_json(
                f"{API}/x/v2/subtitle/web/view?oid={aid}&pid={aid}"
                f"&context_ext={ext}&type=1&cur_production_type=0"
                f"&preferred_language=ai-zh&playlist_switch=0", cred)
            subs = ((p2.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            src = "web/view"
        except Exception:
            subs = []
    if not subs:
        return [], "", ""
    pick = (next((s for s in subs if s.get("lan") == "ai-zh"), None)
            or next((s for s in subs if s.get("lan") in ("zh-CN", "zh-Hans")), None)
            or next((s for s in subs if str(s.get("lan", "")).startswith("zh")), None)
            or subs[0])
    url = str(pick.get("subtitle_url") or "")
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https:" + url
    if not url:
        return [], str(pick.get("lan") or ""), src
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers(cred, f"https://www.bilibili.com/video/{bvid}/"),
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
                body = (await r.json(content_type=None)).get("body") or []
        lines = [{"from": float(x.get("from") or 0), "to": float(x.get("to") or 0),
                  "content": str(x.get("content") or "")} for x in body if x.get("content")]
        return lines, str(pick.get("lan") or ""), src
    except Exception:
        return [], str(pick.get("lan") or ""), src


async def danmaku(cid: int, cred: dict | None = None) -> list[tuple[float, str]]:
    if not cid:
        return []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://comment.bilibili.com/{cid}.xml",
                             headers={"user-agent": UA}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
        out = []
        for m in re.finditer(r'<d p="([^"]*)"[^>]*>([^<]*)</d>', text):
            p = m.group(1).split(",")
            try:
                t = float(p[0])
            except Exception:
                continue
            out.append((t, html.unescape(m.group(2)).strip()))
        return out
    except Exception:
        return []


async def comments(data: dict, cred: dict | None, limit: int = 8) -> list[dict]:
    aid = data.get("aid") or 0
    if not aid:
        return []
    try:
        d = await get_json(f"{API}/x/v2/reply?type=1&oid={aid}&sort=2&ps=20&pn=1", cred,
                           referer=f"https://www.bilibili.com/video/{data.get('bvid','')}/")
        rows = (d.get("data") or {}).get("replies") or []
        out = []
        for r in rows:
            out.append({"uname": str((r.get("member") or {}).get("uname") or "?"),
                        "rpid": str(r.get("rpid_str") or r.get("rpid") or ""),
                        "like": int(r.get("like") or 0),
                        "message": str((r.get("content") or {}).get("message") or "").strip()})
        out.sort(key=lambda x: x["like"], reverse=True)
        return out[:limit]
    except Exception:
        return []


async def playurl(data: dict, cred: dict | None, qn: int = 64, fnval: int = 1) -> dict:
    bvid = data.get("bvid") or ""
    cid = data.get("cid") or (data.get("pages") or [{}])[0].get("cid") or 0
    url = (f"{API}/x/player/playurl?bvid={bvid}&cid={cid}&qn={qn}"
           f"&fnval={fnval}&platform=pc&high_quality=1")
    return await get_json(url, cred, referer=f"https://www.bilibili.com/video/{bvid}/")


async def qr_generate() -> dict:
    d = await get_json(f"{PASSPORT}/x/passport-login/web/qrcode/generate",
                       referer="https://www.bilibili.com/")
    if d.get("code") != 0:
        raise RuntimeError(f"生成二维码失败 code={d.get('code')} {d.get('message')}")
    return d.get("data") or {}


def _parse_cookies(set_cookies: list[str], data_url: str = "") -> dict:
    out = {}
    if data_url:
        from urllib.parse import urlparse, parse_qsl
        try:
            for k, v in parse_qsl(urlparse(data_url).query):
                if k:
                    out[k] = v
        except Exception:
            pass
    for sc in set_cookies or []:
        m = re.match(r"^([^=]+)=([^;]*)", sc)
        if m:
            out[m.group(1).strip()] = m.group(2)
    return out


async def qr_poll(key: str) -> tuple[int, dict, str]:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{PASSPORT}/x/passport-login/web/qrcode/poll?qrcode_key={quote(key)}",
                         headers={"user-agent": UA, "referer": "https://www.bilibili.com/"},
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            cookies = r.headers.getall("Set-Cookie", [])
            d = await r.json(content_type=None)
    data = d.get("data") or {}
    return int(data.get("code") if data.get("code") is not None else -1), \
        _parse_cookies(cookies, str(data.get("url") or "")), str(data.get("url") or "")


async def buvid3() -> str:
    try:
        d = await get_json(f"{API}/x/frontend/finger/spi", referer="https://www.bilibili.com/")
        return str((d.get("data") or {}).get("b_3") or "")
    except Exception:
        return ""


def _aid(data: dict) -> int:
    return int(data.get("aid") or 0)


async def like(data: dict, cred: dict, unlike: bool = False) -> dict:
    # B 站这个接口：1=点赞，2=取消点赞（0 会返回 -400 请求错误）
    return await post_form(f"{API}/x/web-interface/archive/like",
                           {"aid": _aid(data), "like": 2 if unlike else 1,
                            "csrf": cred.get("bili_jct", "")}, cred,
                           referer=f"https://www.bilibili.com/video/{data.get('bvid','')}/")


async def coin(data: dict, cred: dict, count: int = 1) -> dict:
    return await post_form(f"{API}/x/web-interface/coin/add",
                           {"aid": _aid(data), "multiply": max(1, min(2, int(count))),
                            "select_like": 1, "csrf": cred.get("bili_jct", "")}, cred)


async def comment_add(data: dict, cred: dict, message: str,
                      root: int | None = None, parent: int | None = None) -> dict:
    form = {"oid": _aid(data), "type": 1, "message": message,
            "csrf": cred.get("bili_jct", "")}
    if root:
        form["root"] = root
    if parent:
        form["parent"] = parent
    return await post_form(f"{API}/x/v2/reply/add", form, cred,
                           referer=f"https://www.bilibili.com/video/{data.get('bvid','')}/")


async def dynamic_post(text: str, cred: dict, pics: list[dict] | None = None) -> dict:
    mixin = await wbi_mixin(cred)
    q = wbi_query({"csrf": cred.get("bili_jct", "")}, mixin)
    dyn = {
        "content": {"contents": [{"raw_text": text, "type": 1, "biz_id": ""}]},
        "scene": 1,
        "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
    }
    if pics:
        dyn["pics"] = pics[:9]
        dyn["scene"] = 2
    body = {"dyn_req": dyn}
    return await post_json(f"{API}/x/dynamic/feed/create/dyn?{q}", body, cred,
                           origin="https://t.bilibili.com")


async def dynamic_delete(dyn_id: str, cred: dict) -> dict:
    return await post_form("https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/rm_dynamic",
                           {"dynamic_id": dyn_id, "csrf": cred.get("bili_jct", "")},
                           cred, referer=f"https://t.bilibili.com/{dyn_id}")


async def find_video_dynamic(bvid: str, up_mid: str, cred: dict | None,
                             pages: int = 2) -> dict | None:
    """在 UP 主空间动态里找这条视频已有的 AV 卡片，用于转发成带卡片的动态。"""
    if not up_mid:
        return None
    for p in range(max(1, pages)):
        offset = "" if p == 0 else f"&offset={p * 20}"
        url = (f"{API}/x/polymer/web-dynamic/v1/feed/space?host_mid={up_mid}{offset}")
        try:
            d = await get_json(url, cred,
                               referer=f"https://space.bilibili.com/{up_mid}/dynamic")
        except Exception:
            return None
        items = (d.get("data") or {}).get("items") or []
        for it in items:
            arch = ((((it.get("modules") or {}).get("module_dynamic") or {})
                     .get("major") or {}).get("archive") or {})
            if str(arch.get("bvid") or "") == bvid:
                return {"id": str(it.get("id_str") or ""), "title": str(arch.get("title") or "")}
        if not items:
            break
    return None


async def forward_dynamic(orig_id: str, text: str, cred: dict) -> dict:
    return await post_multipart(
        "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/create",
        {"dynamic_id": orig_id, "content": text, "csrf": cred.get("bili_jct", ""),
         "csrf_token": cred.get("bili_jct", ""), "type": 1}, cred)


async def notifications(kind: str, cred: dict, pages: int = 1) -> list[dict]:
    """B 站收件箱：kind=reply（回复我的）/ at（@我的）。只读。"""
    out = []
    cursor = ""
    for _ in range(max(1, pages)):
        url = f"{API}/x/msgfeed/{kind}?platform=web&build=0&mobi_app=web"
        if cursor:
            url += "&cursor=" + quote(cursor)
        d = await get_json(url, cred, referer="https://message.bilibili.com/")
        if int(d.get("code") or 0) != 0:
            raise RuntimeError(f"{kind} code={d.get('code')} {d.get('message')}")
        data = d.get("data") or {}
        items = data.get("items") or []
        for it in items:
            item = it.get("item") or {}
            user = it.get("user") or {}
            out.append({
                "kind": kind, "id": str(it.get("id") or ""),
                "t": int(it.get("reply_time") or it.get("at_time") or 0),
                "who": str(user.get("nickname") or "?"),
                "mid": str(user.get("mid") or ""),
                "text": str(item.get("source_content") or item.get("title") or ""),
                "uri": str(item.get("uri") or ""),
                "business": int(item.get("business_id") or 1),
                "oid": str(item.get("subject_id") or ""),
            })
        cursor = str(data.get("cursor") or "")
        if not cursor or not items:
            break
    seen = set()
    return [x for x in out if not (x["id"] in seen or seen.add(x["id"]))]


# =====================================================================
# 外围内容：搜索/热门/UP/关注/收藏/历史/稍后再看/番剧/专栏/动态/音频
# =====================================================================

TAG_RE = re.compile(r"<[^>]+>")
EP_RE = re.compile(r"(?:ep|EP)(\d{4,})")
SS_RE = re.compile(r"(?:ss|SS)(\d{4,})")
CV_RE = re.compile(r"(?:cv|CV)(\d{4,})")
AU_RE = re.compile(r"(?:au|AU)(\d{4,})")
DYN_RE = re.compile(r"(?:t\.bilibili\.com/|bilibili\.com/opus/|dynamic/)(\d{10,})")
LIVE_RE = re.compile(r"live\.bilibili\.com/(?:h5/)?(\d{1,10})")
UP_RE = re.compile(r"space\.bilibili\.com/(\d{3,})")
BILI_URL_RE = re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/[^\s]+")


def clean_text(s: str) -> str:
    return html.unescape(TAG_RE.sub("", str(s or ""))).strip()


async def wbi_get(path: str, params: dict, cred: dict | None,
                  referer: str = REF) -> dict:
    mixin = await wbi_mixin(cred)
    q = wbi_query(params, mixin)
    sep = "&" if "?" in path else "?"
    return await get_json(f"{path}{sep}{q}", cred, referer)


async def expand_short(text: str) -> str:
    """把 b23.tv 短链展开成最终 URL；失败原样返回。"""
    m = B23_RE.search(text or "")
    if not m:
        return text
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(m.group(0), headers={"user-agent": UA},
                             allow_redirects=True,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                return str(r.url)
    except Exception:
        return text


def detect_kind(text: str) -> tuple[str, str]:
    text = text or ""
    if BV_RE.search(text):
        return "video", BV_RE.search(text).group(0)
    if AV_RE.search(text):
        return "video", "av" + AV_RE.search(text).group(1)
    if EP_RE.search(text):
        return "bangumi", EP_RE.search(text).group(1)
    if SS_RE.search(text):
        return "bangumi", "ss" + SS_RE.search(text).group(1)
    if CV_RE.search(text):
        return "article", CV_RE.search(text).group(1)
    if AU_RE.search(text):
        return "audio", AU_RE.search(text).group(1)
    if DYN_RE.search(text):
        return "dynamic", DYN_RE.search(text).group(1)
    if LIVE_RE.search(text):
        return "live", LIVE_RE.search(text).group(1)
    if UP_RE.search(text):
        return "up", UP_RE.search(text).group(1)
    return "", ""


def parse_page(text: str) -> int:
    m = re.search(r"[?&]p=(\d{1,3})", text or "")
    return max(1, int(m.group(1))) if m else 1


async def search_videos(keyword: str, cred: dict | None, page: int = 1,
                        limit: int = 10) -> list[dict]:
    d = await wbi_get(f"{API}/x/web-interface/wbi/search/type",
                      {"search_type": "video", "keyword": keyword, "page": page},
                      cred, "https://search.bilibili.com/")
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"搜索失败 code={d.get('code')} {d.get('message')}")
    rows = (d.get("data") or {}).get("result") or []
    out = []
    for r in rows[:limit]:
        out.append({
            "bvid": str(r.get("bvid") or ""),
            "title": clean_text(r.get("title")),
            "author": clean_text(r.get("author")),
            "mid": str(r.get("mid") or ""),
            "play": int(r.get("play") or 0),
            "danmaku": int(r.get("video_review") or 0),
            "duration": str(r.get("duration") or ""),
            "pubdate": int(r.get("pubdate") or 0),
            "desc": clean_text(r.get("description")),
        })
    return out


async def hot_videos(cred: dict | None, limit: int = 10) -> list[dict]:
    d = await get_json(f"{API}/x/web-interface/ranking/v2?rid=0&type=all", cred)
    rows = (d.get("data") or {}).get("list") or []
    out = []
    for r in rows[:limit]:
        stt = r.get("stat") or {}
        out.append({
            "bvid": str(r.get("bvid") or ""),
            "title": clean_text(r.get("title")),
            "author": clean_text((r.get("owner") or {}).get("name")),
            "mid": str((r.get("owner") or {}).get("mid") or ""),
            "play": int(stt.get("view") or 0),
            "like": int(stt.get("like") or 0),
            "duration": int(r.get("duration") or 0),
        })
    return out


async def up_info(mid: str, cred: dict | None) -> dict:
    d = await get_json(f"{API}/x/web-interface/card?mid={quote(str(mid))}", cred,
                       referer=f"https://space.bilibili.com/{mid}")
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"UP 查询失败 code={d.get('code')} {d.get('message')}")
    card = (d.get("data") or {}).get("card") or {}
    return {
        "mid": str(card.get("mid") or mid),
        "name": clean_text(card.get("name")),
        "sign": clean_text(card.get("sign")),
        "fans": int(card.get("fans") or 0),
        "level": int((card.get("level_info") or {}).get("current_level") or 0),
        "face": str(card.get("face") or ""),
    }


async def up_videos(mid: str, cred: dict | None, limit: int = 10) -> list[dict]:
    try:
        d = await wbi_get(f"{API}/x/space/wbi/arc/search",
                          {"mid": mid, "pn": 1, "ps": limit, "order": "pubdate"},
                          cred, f"https://space.bilibili.com/{mid}/video")
    except Exception:
        d = await get_json(f"{API}/x/space/arc/search?mid={mid}&pn=1&ps={limit}&order=pubdate",
                           cred, f"https://space.bilibili.com/{mid}/video")
    if int(d.get("code") or 0) != 0:
        # 空间接口 -412 风控时，用搜索按 UP 名兜底
        try:
            u = await up_info(mid, cred)
            rows = await search_videos(u.get("name") or "", cred, 1, max(limit * 3, 12))
            rows = [r for r in rows if str(r.get("mid")) == str(mid)]
            return [{
                "bvid": r["bvid"], "title": r["title"], "play": r["play"],
                "length": r["duration"], "created": r["pubdate"],
            } for r in rows[:limit]]
        except Exception:
            raise RuntimeError(f"UP 投稿查询失败 code={d.get('code')} {d.get('message')}")
    rows = ((d.get("data") or {}).get("list") or {}).get("vlist") or []
    return [{
        "bvid": str(r.get("bvid") or ""),
        "title": clean_text(r.get("title")),
        "play": int(r.get("play") or 0),
        "length": str(r.get("length") or ""),
        "created": int(r.get("created") or 0),
    } for r in rows[:limit]]


async def relation(mid: str, cred: dict | None) -> dict:
    d = await get_json(f"{API}/x/relation?fid={quote(str(mid))}", cred,
                       referer=f"https://space.bilibili.com/{mid}")
    return d.get("data") or {}


async def follow(mid: str, cred: dict, cancel: bool = False) -> dict:
    return await post_form(f"{API}/x/relation/modify",
                           {"fid": mid, "act": 2 if cancel else 1,
                            "re_src": 11, "csrf": cred.get("bili_jct", "")},
                           cred, referer=f"https://space.bilibili.com/{mid}")


async def fav_folders(cred: dict) -> list[dict]:
    mid = str(cred.get("mid") or "")
    d = await get_json(f"{API}/x/v3/fav/folder/created/list-all?up_mid={quote(mid)}&type=2",
                       cred, "https://www.bilibili.com/account/favlist")
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"收藏夹查询失败 code={d.get('code')} {d.get('message')}")
    return [{
        "id": str(x.get("id") or ""),
        "title": clean_text(x.get("title")),
        "count": int(x.get("media_count") or 0),
        "attr": int(x.get("attr") or 0),
    } for x in ((d.get("data") or {}).get("list") or [])]


def pick_folder(folders: list[dict], name: str = "") -> dict:
    name = (name or "").strip()
    if name:
        for f in folders:
            if f["title"] == name or name in f["title"]:
                return f
        raise RuntimeError(f"没有找到收藏夹「{name}」")
    for f in folders:
        if "默认" in f["title"]:
            return f
    if folders:
        return folders[0]
    raise RuntimeError("这个号还没有收藏夹")


async def fav_deal(data: dict, cred: dict, folder: str = "",
                   cancel: bool = False) -> tuple[dict, dict]:
    folders = await fav_folders(cred)
    f = pick_folder(folders, folder)
    form = {"rid": _aid(data), "type": 2,
            "add_media_ids": "" if cancel else f["id"],
            "del_media_ids": f["id"] if cancel else "",
            "csrf": cred.get("bili_jct", "")}
    r = await post_form(f"{API}/x/v3/fav/resource/deal", form, cred,
                        referer=f"https://www.bilibili.com/video/{data.get('bvid','')}/")
    return r, f


async def fav_list(cred: dict, folder: str = "", page: int = 1,
                   limit: int = 20) -> tuple[dict, list[dict]]:
    f = pick_folder(await fav_folders(cred), folder)
    d = await get_json(f"{API}/x/v3/fav/resource/list?media_id={f['id']}&pn={page}&ps={limit}&platform=web",
                       cred, "https://www.bilibili.com/account/favlist")
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"收藏夹内容读取失败 code={d.get('code')} {d.get('message')}")
    rows = (d.get("data") or {}).get("medias") or []
    out = [{
        "bvid": str(r.get("bvid") or ""),
        "title": clean_text(r.get("title")),
        "author": clean_text((r.get("upper") or {}).get("name")),
        "play": int(((r.get("cnt_info") or {}).get("play")) or 0),
    } for r in rows if r.get("bvid")]
    return f, out


async def history(cred: dict, limit: int = 20) -> list[dict]:
    d = await get_json(f"{API}/x/web-interface/history/cursor?ps={limit}&type=archive", cred,
                       "https://www.bilibili.com/account/history")
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"历史记录读取失败 code={d.get('code')} {d.get('message')}")
    rows = (d.get("data") or {}).get("list") or []
    out = []
    for r in rows:
        h = r.get("history") or {}
        out.append({
            "bvid": str(h.get("bvid") or ""),
            "title": clean_text(r.get("title")),
            "author": clean_text(r.get("author_name")),
            "progress": int(r.get("progress") or 0),
            "duration": int(r.get("duration") or 0),
            "view_at": int(r.get("view_at") or 0),
        })
    return out


async def watch_later(cred: dict) -> list[dict]:
    d = await get_json(f"{API}/x/v2/history/toview", cred,
                       "https://www.bilibili.com/watchlater/")
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"稍后再看读取失败 code={d.get('code')} {d.get('message')}")
    rows = (d.get("data") or {}).get("list") or []
    return [{
        "bvid": str(r.get("bvid") or ""),
        "title": clean_text(r.get("title")),
        "author": clean_text((r.get("owner") or {}).get("name")),
        "duration": int(r.get("duration") or 0),
    } for r in rows]


async def watch_later_set(data: dict, cred: dict, cancel: bool = False) -> dict:
    path = "del" if cancel else "add"
    return await post_form(f"{API}/x/v2/history/toview/{path}",
                           {"aid": _aid(data), "csrf": cred.get("bili_jct", "")},
                           cred, "https://www.bilibili.com/watchlater/")


async def season(ep_id: str = "", season_id: str = "") -> dict:
    if ep_id:
        url = f"{API}/pgc/view/web/season?ep_id={quote(str(ep_id))}"
    else:
        url = f"{API}/pgc/view/web/season?season_id={quote(str(season_id))}"
    d = await get_json(url)
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"番剧查询失败 code={d.get('code')} {d.get('message')}")
    res = d.get("result") or {}
    eps = res.get("episodes") or []
    return {
        "season_id": str(res.get("season_id") or season_id),
        "title": clean_text(res.get("title")),
        "cover": str(res.get("cover") or ""),
        "evaluate": clean_text(res.get("evaluate")),
        "rating": res.get("rating") or {},
        "episodes": [{
            "ep_id": str(e.get("id") or ""),
            "aid": e.get("aid") or 0,
            "bvid": str(e.get("bvid") or ""),
            "cid": e.get("cid") or 0,
            "title": clean_text(e.get("title")),
            "long_title": clean_text(e.get("long_title")),
            "duration": int(e.get("duration") or 0),
        } for e in eps],
    }


async def playurl_pgc(ep: dict, cred: dict | None, qn: int = 64, fnval: int = 1) -> dict:
    url = (f"{API}/pgc/player/web/playurl?ep_id={ep.get('ep_id')}"
           f"&cid={ep.get('cid')}&avid={ep.get('aid')}&qn={qn}&fnval={fnval}&fourk=1")
    return await get_json(url, cred,
                          f"https://www.bilibili.com/bangumi/play/ep{ep.get('ep_id')}")


async def article_info(cv_id: str, cred: dict | None) -> dict:
    cv_id = str(cv_id)
    info = await get_json(f"{API}/x/article/viewinfo?id={quote(cv_id)}", cred,
                          f"https://www.bilibili.com/read/cv{cv_id}")
    view = {}
    for url in (f"{API}/x/article/view?aid={quote(cv_id)}",
                f"{API}/x/article/view?id={quote(cv_id)}"):
        try:
            d = await get_json(url, cred, f"https://www.bilibili.com/read/cv{cv_id}")
            if int(d.get("code") or 0) == 0:
                view = d
                break
        except Exception:
            pass
    if int(info.get("code") or 0) != 0 and not view:
        raise RuntimeError(f"专栏查询失败 code={info.get('code')} {info.get('message')}")
    i = info.get("data") or {}
    v = view.get("data") or {}
    stats = i.get("stats") or v.get("stats") or {}
    title = i.get("title") or v.get("title") or ""
    author = i.get("author_name") or (v.get("author") or {}).get("name") or ""
    mid = i.get("mid") or (v.get("author") or {}).get("mid") or ""
    content = v.get("content") or i.get("content") or ""
    return {
        "cv_id": cv_id,
        "title": clean_text(title),
        "author": clean_text(author),
        "mid": str(mid or ""),
        "text": clean_text(content)[:4000],
        "view": int(stats.get("view") or 0),
        "like": int(stats.get("like") or 0),
        "reply": int(stats.get("reply") or 0),
    }


async def dynamic_detail(dyn_id: str, cred: dict | None) -> dict:
    d = await get_json(
        f"{API}/x/polymer/web-dynamic/v1/detail?id={quote(str(dyn_id))}"
        "&features=itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
        cred, f"https://t.bilibili.com/{dyn_id}")
    if int(d.get("code") or 0) != 0:
        # 新版 detail 偶尔 4101139，退到旧 dynamic_svr
        old = await get_json("https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/get_dynamic_detail"
                             f"?dynamic_id={quote(str(dyn_id))}", cred,
                             f"https://t.bilibili.com/{dyn_id}")
        if int(old.get("code") or 0) != 0:
            raise RuntimeError(f"动态查询失败 code={d.get('code')} {d.get('message')}")
        card = (old.get("data") or {}).get("card") or {}
        try:
            card = json.loads(card) if isinstance(card, str) else card
        except Exception:
            card = {}
        item = card.get("item") or {}
        desc = (item.get("description") or item.get("content") or "")
        pics = [str(x.get("img_src") or x.get("src") or "")
                for x in (item.get("pictures") or []) if x]
        user = card.get("user") or {}
        return {
            "dyn_id": str(dyn_id), "author": clean_text(user.get("name")),
            "text": clean_text(desc), "title": clean_text(item.get("title")),
            "bvid": str(item.get("bvid") or ""), "images": pics[:9],
        }
    item = (d.get("data") or {}).get("item") or {}
    md = ((item.get("modules") or {}).get("module_dynamic") or {})
    desc = (md.get("desc") or {}).get("text") or ""
    major = md.get("major") or {}
    title = ""
    bvid = ""
    if major.get("archive"):
        title = clean_text(major["archive"].get("title"))
        bvid = str(major["archive"].get("bvid") or "")
    elif major.get("article"):
        art = major["article"]
        title = clean_text(art.get("title"))
        desc = desc or art.get("desc") or ""
    elif major.get("opus"):
        op = major["opus"]
        title = clean_text(op.get("title"))
        desc = desc or ((op.get("summary") or {}).get("text") or "")
    imgs = []
    draw = (major.get("draw") or {}).get("items") or []
    imgs = [str(x.get("src") or "") for x in draw if x.get("src")]
    author = clean_text(((item.get("modules") or {}).get("module_author") or {}).get("name"))
    return {
        "dyn_id": str(dyn_id), "author": author, "text": clean_text(desc),
        "title": title, "bvid": bvid, "images": imgs[:9],
    }


async def audio_info(au_id: str, cred: dict | None) -> dict:
    d = await get_json(f"https://www.bilibili.com/audio/music-service-c/web/song/info?sid={quote(str(au_id))}",
                       cred, f"https://www.bilibili.com/audio/au{au_id}")
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"音频查询失败 code={d.get('code')} {d.get('message')}")
    data = d.get("data") or {}
    return {
        "au_id": str(au_id),
        "title": clean_text(data.get("title")),
        "author": clean_text(data.get("author")),
        "duration": int(data.get("duration") or 0),
        "cover": str(data.get("cover") or ""),
    }


# =====================================================================
# 私信
# =====================================================================

async def dm_sessions(cred: dict, size: int = 20) -> list[dict]:
    d = await get_json("https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions"
                       f"?session_type=1&group_id=0&size={size}", cred,
                       "https://message.bilibili.com/")
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"私信会话读取失败 code={d.get('code')} {d.get('message')}")
    rows = (d.get("data") or {}).get("session_list") or []
    out = []
    for r in rows:
        last = r.get("last_msg") or {}
        content = last.get("content") or ""
        try:
            obj = json.loads(content)
            content = obj.get("content") or obj.get("text") or obj.get("title") or content
        except Exception:
            pass
        out.append({
            "talker_id": str(r.get("talker_id") or ""),
            "unread": int(r.get("unread_count") or 0),
            "timestamp": int(last.get("timestamp") or 0),
            "last": str(content)[:80],
        })
    return out


async def dm_messages(talker_id: str, cred: dict, size: int = 20) -> list[dict]:
    d = await get_json("https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs"
                       f"?talker_id={quote(str(talker_id))}&session_type=1&size={size}", cred,
                       "https://message.bilibili.com/")
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"私信读取失败 code={d.get('code')} {d.get('message')}")
    rows = (d.get("data") or {}).get("messages") or []
    out = []
    for r in rows:
        content = r.get("content") or ""
        try:
            obj = json.loads(content)
            content = obj.get("content") or obj.get("text") or obj.get("title") or content
        except Exception:
            pass
        out.append({
            "sender": str(r.get("sender_uid") or ""),
            "timestamp": int(r.get("timestamp") or 0),
            "content": str(content)[:500],
        })
    out.sort(key=lambda x: x["timestamp"])
    return out


async def dm_send(to_uid: str, text: str, cred: dict) -> dict:
    mid = str(cred.get("mid") or "")
    dev = str(cred.get("buvid3") or mid or int(time.time()))
    mixin = await wbi_mixin(cred)
    q = wbi_query({"w_sender_uid": mid, "w_receiver_id": to_uid, "w_dev_id": dev}, mixin)
    form = {
        "msg[sender_uid]": mid,
        "msg[receiver_type]": 1,
        "msg[receiver_id]": to_uid,
        "msg[msg_type]": 1,
        "msg[msg_status]": 0,
        "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
        "msg[dev_id]": dev,
        "msg[timestamp]": int(time.time()),
        "from_firework": 0,
        "build": 0,
        "mobi_app": "web",
        "csrf": cred.get("bili_jct", ""),
    }
    return await post_form(f"https://api.vc.bilibili.com/web_im/v1/web_im/send_msg?{q}",
                           form, cred, "https://message.bilibili.com/")


# =====================================================================
# 直播
# =====================================================================

async def live_room_info(room_id: str, cred: dict | None = None) -> dict:
    d = await get_json(f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={quote(str(room_id))}",
                       cred, f"https://live.bilibili.com/{room_id}")
    if int(d.get("code") or 0) != 0:
        raise RuntimeError(f"直播间查询失败 code={d.get('code')} {d.get('message')}")
    data = d.get("data") or {}
    return {
        "room_id": str(data.get("room_id") or room_id),
        "short_id": str(data.get("short_id") or ""),
        "uid": str(data.get("uid") or ""),
        "title": clean_text(data.get("title")),
        "live_status": int(data.get("live_status") or 0),
        "online": int(data.get("online") or 0),
        "area": clean_text(data.get("area_name")),
    }


async def live_danmu_info(room_id: str, cred: dict | None = None) -> dict:
    d = await get_json("https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
                       f"?id={quote(str(room_id))}&type=0", cred,
                       f"https://live.bilibili.com/{room_id}")
    if int(d.get("code") or 0) == 0 and d.get("data"):
        return d.get("data") or {}
    # 新接口被 -352 风控时，退到旧 getConf
    c = await get_json("https://api.live.bilibili.com/room/v1/Danmu/getConf"
                       f"?room_id={quote(str(room_id))}&platform=pc&player=web", cred,
                       f"https://live.bilibili.com/{room_id}")
    if int(c.get("code") or 0) != 0:
        raise RuntimeError(f"弹幕服务器查询失败 code={d.get('code')} {d.get('message')} / "
                           f"fallback {c.get('code')} {c.get('message')}")
    data = c.get("data") or {}
    host = str(data.get("host") or data.get("host_server_list", [{}])[0].get("host") or "")
    port = int(data.get("wss_port") or 443)
    if not host:
        raise RuntimeError("弹幕服务器地址为空")
    return {"token": data.get("token") or "", "host_list": [{"host": host, "wss_port": port}]}


async def live_send_danmaku(room_id: str, text: str, cred: dict) -> dict:
    room = await live_room_info(room_id, cred)
    real = str(room.get("room_id") or room_id)
    form = {
        "color": 16777215, "fontsize": 25, "mode": 1,
        "msg": text, "plat": 1, "rnd": int(time.time()),
        "roomid": real,
        "csrf": cred.get("bili_jct", ""),
        "csrf_token": cred.get("bili_jct", ""),
    }
    return await post_form("https://api.live.bilibili.com/msg/send", form, cred,
                           f"https://live.bilibili.com/{real}")


# =====================================================================
# 动态图片
# =====================================================================

async def upload_dynamic_image(path: str, cred: dict) -> dict:
    form = aiohttp.FormData()
    with open(path, "rb") as f:
        raw = f.read()
    form.add_field("file_up", raw, filename="image.jpg",
                   content_type="image/jpeg")
    form.add_field("category", "daily")
    form.add_field("biz", "new_dyn")
    form.add_field("csrf", cred.get("bili_jct", ""))
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API}/x/dynamic/feed/draw/upload_bfs",
                          data=form, headers=headers(cred, "https://t.bilibili.com/"),
                          timeout=aiohttp.ClientTimeout(total=60)) as r:
            data = await r.json(content_type=None)
    if int(data.get("code") or 0) != 0:
        raise RuntimeError(f"图片上传失败 code={data.get('code')} {data.get('message')}")
    d = data.get("data") or {}
    src = str(d.get("image_url") or d.get("url") or "")
    if not src:
        raise RuntimeError(f"图片上传没有返回 URL：{data}")
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        w, h = 0, 0
    size_kb = round(os.path.getsize(path) / 1024, 1) if os.path.exists(path) else 0
    return {"img_src": src, "img_width": w, "img_height": h, "img_size": size_kb}
