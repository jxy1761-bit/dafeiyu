"""Merge AstrBot main-database conversations and personas.

Conversation history is a JSON list without per-message timestamps. The merge is
therefore conservative: an order-preserving superset wins; if both sides have
genuinely different branches, the newer conversation wins. This avoids creating
an incoherent history with split tool-call pairs.
"""

from __future__ import annotations

import json
from typing import Any

from .common import to_ts, json_dumps


def _ts(row: dict[str, Any]) -> float:
    return max(to_ts(row.get("updated_at")), to_ts(row.get("created_at")))


def _msg_keys(messages: list[Any]) -> list[str]:
    return [json_dumps(m) for m in messages]


def _is_subsequence(short: list[str], long: list[str]) -> bool:
    index = 0
    for item in long:
        if index < len(short) and short[index] == item:
            index += 1
    return index == len(short)


def merge_history(a: list[Any], b: list[Any], ta: float, tb: float) -> list[Any]:
    if a == b:
        return a
    ka, kb = _msg_keys(a), _msg_keys(b)
    if len(ka) <= len(kb) and kb[: len(ka)] == ka:
        return b
    if len(kb) <= len(ka) and ka[: len(kb)] == kb:
        return a
    if _is_subsequence(ka, kb):
        return b
    if _is_subsequence(kb, ka):
        return a
    return a if ta >= tb else b


def _conversation_row(cid: str, srow: dict | None, crow: dict | None) -> dict[str, Any]:
    if srow and crow:
        sts, cts = _ts(srow), _ts(crow)
        newer, older = (srow, crow) if sts >= cts else (crow, srow)
        content = merge_history(srow.get("content") or [], crow.get("content") or [], sts, cts)
        created = srow.get("created_at") if to_ts(srow.get("created_at")) <= to_ts(crow.get("created_at")) else crow.get("created_at")
        updated = srow.get("updated_at") if to_ts(srow.get("updated_at")) >= to_ts(crow.get("updated_at")) else crow.get("updated_at")
        return {
            "conversation_id": cid,
            "platform_id": newer.get("platform_id") or older.get("platform_id"),
            "user_id": newer.get("user_id") or older.get("user_id"),
            "content": content,
            "title": newer.get("title"),
            "persona_id": newer.get("persona_id"),
            "token_usage": max(int(srow.get("token_usage") or 0), int(crow.get("token_usage") or 0)),
            "created_at": created,
            "updated_at": updated,
        }
    row = srow or crow
    return {
        "conversation_id": cid,
        "platform_id": row.get("platform_id"),
        "user_id": row.get("user_id"),
        "content": row.get("content") or [],
        "title": row.get("title"),
        "persona_id": row.get("persona_id"),
        "token_usage": int(row.get("token_usage") or 0),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def merge_astrbot(server: dict[str, Any] | None, client: dict[str, Any] | None,
                  tombstones: dict[str, dict[str, float]]) -> dict[str, Any]:
    server = server or {}
    client = client or {}
    dead_conv = tombstones.get("astrbot_conversations", {})
    dead_personas = tombstones.get("astrbot_personas", {})
    dead_folders = tombstones.get("astrbot_persona_folders", {})

    s_rows = {r["conversation_id"]: r for r in server.get("conversations", [])
              if r.get("conversation_id") not in dead_conv}
    c_rows = {r["conversation_id"]: r for r in client.get("conversations", [])
              if r.get("conversation_id") not in dead_conv}

    groups = []  # [cid, srow, crow]
    client_only = []
    server_only = []
    for cid in sorted(set(s_rows) | set(c_rows)):
        srow, crow = s_rows.get(cid), c_rows.get(cid)
        if srow:
            groups.append([cid, srow, crow])
            if not crow:
                server_only.append(cid)
        else:
            client_only.append(cid)

    # Same UMO on both sides but different UUID: merge into the server row.
    by_user_s = {}
    for cid in server_only:
        key = (s_rows[cid].get("platform_id"), s_rows[cid].get("user_id"))
        by_user_s.setdefault(key, []).append(cid)
    by_user_c = {}
    for cid in client_only:
        key = (c_rows[cid].get("platform_id"), c_rows[cid].get("user_id"))
        by_user_c.setdefault(key, []).append(cid)
    merged_client = set()
    for key, scids in by_user_s.items():
        ccids = by_user_c.get(key) or []
        if len(scids) == 1 and len(ccids) == 1:
            scid, ccid = scids[0], ccids[0]
            for group in groups:
                if group[0] == scid:
                    group[2] = c_rows[ccid]
                    break
            merged_client.add(ccid)

    next_inner = max([int(r.get("inner_id") or 0) for r in s_rows.values()] or [0]) + 1
    out_convs = []
    for group in groups:
        cid, srow, crow = group
        row = _conversation_row(cid, srow, crow)
        row["inner_id"] = int(srow["inner_id"]) if srow else next_inner
        if not srow:
            next_inner += 1
        out_convs.append(row)
    for cid in client_only:
        if cid in merged_client:
            continue
        row = _conversation_row(cid, None, c_rows[cid])
        row["inner_id"] = next_inner
        next_inner += 1
        out_convs.append(row)
    out_convs.sort(key=lambda r: r["conversation_id"])

    def merge_named(slist: list[dict], clist: list[dict], key: str, dead: dict, id_field: str) -> list[dict]:
        s_map = {r[key]: r for r in slist if r.get(key) not in dead}
        c_map = {r[key]: r for r in clist if r.get(key) not in dead}
        next_id = max([int(r.get(id_field) or 0) for r in s_map.values()] or [0]) + 1
        out = []
        for name in sorted(set(s_map) | set(c_map)):
            sr, cr = s_map.get(name), c_map.get(name)
            if sr and cr:
                chosen = sr if _ts(sr) >= _ts(cr) else cr
            else:
                chosen = sr or cr
            row = dict(chosen)
            row[id_field] = int(sr[id_field]) if sr else next_id
            if not sr:
                next_id += 1
            out.append(row)
        return out

    personas = merge_named(server.get("personas", []), client.get("personas", []),
                           "persona_id", dead_personas, "id")
    folders = merge_named(server.get("persona_folders", []), client.get("persona_folders", []),
                          "folder_id", dead_folders, "id")
    return {"conversations": out_convs, "personas": personas, "persona_folders": folders}
