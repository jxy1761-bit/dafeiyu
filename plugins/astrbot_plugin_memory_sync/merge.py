"""Deterministic bidirectional merge. The server owns canonical integer IDs."""

from __future__ import annotations

import json
from typing import Any, Callable

from .maindb import merge_astrbot
from .common import (
    SCHEMA, to_ts, meta_ts, json_dumps, parse_json, list_union_json, now_ts,
)


def _merge_tombstones(server: dict | None, client: dict | None) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for group in (server or {}, client or {}):
        for table, keys in group.items():
            bucket = out.setdefault(table, {})
            if isinstance(keys, dict):
                for key, ts in keys.items():
                    bucket[str(key)] = max(bucket.get(str(key), 0.0), to_ts(ts) or now_ts())
            else:
                for key in keys or []:
                    bucket[str(key)] = max(bucket.get(str(key), 0.0), now_ts())
    return out


def _alive(row: dict[str, Any], key: str, tombstones: dict[str, float], tsfn: Callable) -> bool:
    # An explicit deletion is authoritative on both ends, even if clock skew makes
    # the surviving copy look "newer".
    return key not in tombstones


def _choose(sr: dict | None, cr: dict | None, tsfn: Callable) -> tuple[dict | None, str]:
    if sr is None:
        return cr, "c"
    if cr is None:
        return sr, "s"
    return (sr, "s") if tsfn(sr) >= tsfn(cr) else (cr, "c")


def _doc_ts(row: dict) -> float:
    return max(to_ts(row.get("updated_at")), to_ts(row.get("created_at")), meta_ts(row.get("metadata")))


def _atom_ts(row: dict) -> float:
    return max(to_ts(row.get("last_reinforced_at")), to_ts(row.get("last_accessed_at")), to_ts(row.get("created_at")))


def _source_ts(row: dict) -> float:
    return to_ts(row.get("updated_at"))


def _graph_ts(row: dict) -> float:
    return max(to_ts(row.get("updated_at")), to_ts(row.get("created_at")), meta_ts(row.get("metadata")))


def _message_ts(row: dict) -> float:
    return to_ts(row.get("timestamp"))


def _session_ts(row: dict) -> float:
    return to_ts(row.get("last_active_at"))


def _remap_meta(raw: Any, mapping: dict[int, int], keys: set[str]) -> Any:
    if not raw or not mapping:
        return raw
    data = parse_json(raw, None)
    if data is None:
        return raw
    changed = False

    def walk(value: Any) -> Any:
        nonlocal changed
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if k in keys and isinstance(v, (int, str)):
                    try:
                        iv = int(v)
                    except Exception:
                        out[k] = v
                        continue
                    if iv in mapping:
                        out[k] = mapping[iv]
                        changed = True
                        continue
                out[k] = walk(v)
            return out
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    result = walk(data)
    return json_dumps(result) if changed else raw


def _remap_source_json(raw: Any, msg_map: dict[int, int]) -> Any:
    if not raw or not msg_map:
        return raw
    data = parse_json(raw, None)
    if data is None:
        return raw
    changed = False
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                nid = msg_map.get(item["id"])
                if nid is not None:
                    item["id"] = nid
                    changed = True
    return json_dumps(data) if changed else raw


def merge_snapshots(server: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
    sm, cm = server["memory"], client["memory"]
    sc, cc = server["conversations"], client["conversations"]
    dead = _merge_tombstones(server.get("deleted"), client.get("deleted"))

    dead_docs = dead.get("documents", {})
    dead_atoms = dead.get("atoms", {})
    dead_sources = dead.get("sources", {})
    dead_nodes = dead.get("nodes", {})
    dead_edges = dead.get("edges", {})
    dead_entries = dead.get("entries", {})
    dead_gdocs = dead.get("graph_documents", {})
    dead_sessions = dead.get("sessions", {})
    dead_messages = dead.get("messages", {})

    s_docs = {r["doc_id"]: r for r in sm["documents"] if _alive(r, r["doc_id"], dead_docs, _doc_ts)}
    c_docs = {r["doc_id"]: r for r in cm["documents"] if _alive(r, r["doc_id"], dead_docs, _doc_ts)}
    doc_keys = sorted(set(s_docs) | set(c_docs))
    next_doc = max([int(r["id"]) for r in s_docs.values()] or [0]) + 1
    doc_key_to_cid: dict[str, int] = {}
    cdoc: dict[int, int] = {}
    for key in doc_keys:
        sr, cr = s_docs.get(key), c_docs.get(key)
        cid = int(sr["id"]) if sr else next_doc
        if not sr:
            next_doc += 1
        doc_key_to_cid[key] = cid
        if cr:
            cdoc[int(cr["id"])] = cid

    s_atoms = {r["_key"]: r for r in sm["atoms"]
               if r["parent"] in doc_key_to_cid and _alive(r, r["_key"], dead_atoms, _atom_ts)}
    c_atoms = {r["_key"]: r for r in cm["atoms"]
               if r["parent"] in doc_key_to_cid and _alive(r, r["_key"], dead_atoms, _atom_ts)}
    atom_keys = sorted(set(s_atoms) | set(c_atoms))
    next_atom = max([int(r["id"]) for r in s_atoms.values()] or [0]) + 1
    atom_key_to_cid: dict[str, int] = {}
    catom: dict[int, int] = {}
    for key in atom_keys:
        sr, cr = s_atoms.get(key), c_atoms.get(key)
        cid = int(sr["id"]) if sr else next_atom
        if not sr:
            next_atom += 1
        atom_key_to_cid[key] = cid
        if cr:
            catom[int(cr["id"])] = cid

    s_sources = {r["doc_id"]: r for r in sm["sources"]
                 if r["doc_id"] in doc_key_to_cid and _alive(r, r["doc_id"], dead_sources, _source_ts)}
    c_sources = {r["doc_id"]: r for r in cm["sources"]
                 if r["doc_id"] in doc_key_to_cid and _alive(r, r["doc_id"], dead_sources, _source_ts)}

    s_nodes = {r["node_key"]: r for r in sm["nodes"] if _alive(r, r["node_key"], dead_nodes, _graph_ts)}
    c_nodes = {r["node_key"]: r for r in cm["nodes"] if _alive(r, r["node_key"], dead_nodes, _graph_ts)}
    node_keys = sorted(set(s_nodes) | set(c_nodes))
    next_node = max([int(r["id"]) for r in s_nodes.values()] or [0]) + 1
    node_key_to_cid: dict[str, int] = {}
    cnode: dict[int, int] = {}
    for key in node_keys:
        sr, cr = s_nodes.get(key), c_nodes.get(key)
        cid = int(sr["id"]) if sr else next_node
        if not sr:
            next_node += 1
        node_key_to_cid[key] = cid
        if cr:
            cnode[int(cr["id"])] = cid

    def edge_ok(r: dict) -> bool:
        return (r["source_doc"] in doc_key_to_cid and r["source_node"] in node_key_to_cid
                and r["target_node"] in node_key_to_cid and _alive(r, r["edge_key"], dead_edges, _graph_ts))

    s_edges = {r["edge_key"]: r for r in sm["edges"] if edge_ok(r)}
    c_edges = {r["edge_key"]: r for r in cm["edges"] if edge_ok(r)}
    edge_keys = sorted(set(s_edges) | set(c_edges))
    next_edge = max([int(r["id"]) for r in s_edges.values()] or [0]) + 1
    edge_key_to_cid: dict[str, int] = {}
    cedge: dict[int, int] = {}
    for key in edge_keys:
        sr, cr = s_edges.get(key), c_edges.get(key)
        cid = int(sr["id"]) if sr else next_edge
        if not sr:
            next_edge += 1
        edge_key_to_cid[key] = cid
        if cr:
            cedge[int(cr["id"])] = cid

    s_gdocs = {r["doc_id"]: r for r in sm["graph_documents"] if _alive(r, r["doc_id"], dead_gdocs, _graph_ts)}
    c_gdocs = {r["doc_id"]: r for r in cm["graph_documents"] if _alive(r, r["doc_id"], dead_gdocs, _graph_ts)}
    gdoc_keys = sorted(set(s_gdocs) | set(c_gdocs))
    next_gdoc = max([int(r["id"]) for r in s_gdocs.values()] or [0]) + 1
    gdoc_key_to_cid: dict[str, int] = {}
    cgdoc: dict[int, int] = {}
    for key in gdoc_keys:
        sr, cr = s_gdocs.get(key), c_gdocs.get(key)
        cid = int(sr["id"]) if sr else next_gdoc
        if not sr:
            next_gdoc += 1
        gdoc_key_to_cid[key] = cid
        if cr:
            cgdoc[int(cr["id"])] = cid

    def entry_ok(r: dict) -> bool:
        return (r["source_doc"] in doc_key_to_cid
                and (not r["edge_key"] or r["edge_key"] in edge_key_to_cid)
                and (not r["vector_doc"] or r["vector_doc"] in gdoc_key_to_cid)
                and _alive(r, r["entry_key"], dead_entries, _graph_ts))

    s_entries = {r["entry_key"]: r for r in sm["entries"] if entry_ok(r)}
    c_entries = {r["entry_key"]: r for r in cm["entries"] if entry_ok(r)}
    entry_keys = sorted(set(s_entries) | set(c_entries))
    next_entry = max([int(r["id"]) for r in s_entries.values()] or [0]) + 1
    entry_key_to_cid: dict[str, int] = {}
    centry: dict[int, int] = {}
    for key in entry_keys:
        sr, cr = s_entries.get(key), c_entries.get(key)
        cid = int(sr["id"]) if sr else next_entry
        if not sr:
            next_entry += 1
        entry_key_to_cid[key] = cid
        if cr:
            centry[int(cr["id"])] = cid

    out_docs = []
    for key in doc_keys:
        chosen, src = _choose(s_docs.get(key), c_docs.get(key), _doc_ts)
        meta = chosen["metadata"]
        if src == "c":
            meta = _remap_meta(meta, cdoc, {"previous_id", "imported_from_id", "memory_id", "source_memory_id"})
        out_docs.append({"id": doc_key_to_cid[key], "doc_id": key, "text": chosen["text"],
                         "metadata": meta, "created_at": chosen["created_at"], "updated_at": chosen["updated_at"]})

    out_atoms = []
    for key in atom_keys:
        chosen, src = _choose(s_atoms.get(key), c_atoms.get(key), _atom_ts)
        meta = chosen["metadata"]
        if src == "c":
            meta = _remap_meta(meta, cdoc, {"memory_id", "parent_memory_id", "source_memory_id"})
        out_atoms.append({
            "id": atom_key_to_cid[key], "parent": chosen["parent"],
            "atom_type": chosen["atom_type"], "content": chosen["content"], "entities": chosen["entities"],
            "importance": chosen["importance"], "confidence": chosen["confidence"],
            "created_at": chosen["created_at"], "last_accessed_at": chosen["last_accessed_at"],
            "last_reinforced_at": chosen["last_reinforced_at"], "event_time": chosen["event_time"],
            "ttl_days": chosen["ttl_days"], "expires_at": chosen["expires_at"], "status": chosen["status"],
            "reinforcement_count": chosen["reinforcement_count"], "decay_type": chosen["decay_type"],
            "session_id": chosen["session_id"], "persona_id": chosen["persona_id"], "metadata": meta,
            "_key": key,
        })

    out_sources = []
    client_source_rows: list[int] = []
    for key in sorted(set(s_sources) | set(c_sources)):
        chosen, src = _choose(s_sources.get(key), c_sources.get(key), _source_ts)
        if src == "c":
            client_source_rows.append(len(out_sources))
        out_sources.append({"doc_id": key, "source_json": chosen["source_json"],
                            "created_at": chosen["created_at"], "updated_at": chosen["updated_at"]})

    out_nodes = []
    for key in node_keys:
        chosen, _ = _choose(s_nodes.get(key), c_nodes.get(key), _graph_ts)
        out_nodes.append({"id": node_key_to_cid[key], "node_key": key, "node_type": chosen["node_type"],
                          "node_value": chosen["node_value"], "canonical_value": chosen["canonical_value"],
                          "metadata": chosen["metadata"], "created_at": chosen["created_at"],
                          "updated_at": chosen["updated_at"]})

    out_edges = []
    for key in edge_keys:
        chosen, src = _choose(s_edges.get(key), c_edges.get(key), _graph_ts)
        meta = chosen["metadata"]
        if src == "c":
            meta = _remap_meta(meta, cdoc, {"memory_id", "source_memory_id"})
        out_edges.append({
            "id": edge_key_to_cid[key], "edge_key": key,
            "source_node": chosen["source_node"], "target_node": chosen["target_node"],
            "relation_type": chosen["relation_type"], "source_doc": chosen["source_doc"],
            "weight": chosen["weight"], "confidence": chosen["confidence"], "status": chosen["status"],
            "metadata": meta, "created_at": chosen["created_at"], "updated_at": chosen["updated_at"],
        })

    out_entries = []
    for key in entry_keys:
        chosen, src = _choose(s_entries.get(key), c_entries.get(key), _graph_ts)
        meta = chosen["metadata"]
        if src == "c":
            meta = _remap_meta(meta, cdoc, {"memory_id", "source_memory_id"})
        out_entries.append({
            "id": entry_key_to_cid[key], "entry_key": key, "source_doc": chosen["source_doc"],
            "session_id": chosen["session_id"], "persona_id": chosen["persona_id"],
            "entry_type": chosen["entry_type"], "relation_type": chosen["relation_type"],
            "content": chosen["content"], "metadata": meta, "edge_key": chosen["edge_key"],
            "vector_doc": chosen["vector_doc"],
            "created_at": chosen["created_at"], "updated_at": chosen["updated_at"],
        })

    pairs = set()
    for snap in (sm, cm):
        for r in snap["entry_nodes"]:
            if r["entry_key"] in entry_key_to_cid and r["node_key"] in node_key_to_cid:
                pairs.add((r["entry_key"], r["node_key"]))
    out_entry_nodes = [{"entry_key": e, "node_key": n} for e, n in sorted(pairs)]

    out_gdocs = []
    for key in gdoc_keys:
        chosen, src = _choose(s_gdocs.get(key), c_gdocs.get(key), _graph_ts)
        meta = chosen["metadata"]
        if src == "c":
            meta = _remap_meta(meta, cdoc, {"memory_id", "source_memory_id"})
            meta = _remap_meta(meta, catom, {"atom_id"})
        out_gdocs.append({"id": gdoc_key_to_cid[key], "doc_id": key, "text": chosen["text"],
                          "metadata": meta, "created_at": chosen["created_at"], "updated_at": chosen["updated_at"]})

    vectors = {key: sm["vectors"].get(key) or cm["vectors"].get(key) for key in doc_keys
               if sm["vectors"].get(key) or cm["vectors"].get(key)}
    graph_vectors = {key: sm["graph_vectors"].get(key) or cm["graph_vectors"].get(key) for key in gdoc_keys
                     if sm["graph_vectors"].get(key) or cm["graph_vectors"].get(key)}

    s_sessions = {r["session_id"]: r for r in sc["sessions"] if _alive(r, r["session_id"], dead_sessions, _session_ts)}
    c_sessions = {r["session_id"]: r for r in cc["sessions"] if _alive(r, r["session_id"], dead_sessions, _session_ts)}
    session_keys = sorted(set(s_sessions) | set(c_sessions))
    next_session = max([int(r["id"]) for r in s_sessions.values()] or [0]) + 1
    sess_key_to_cid: dict[str, int] = {}
    for key in session_keys:
        sr, cr = s_sessions.get(key), c_sessions.get(key)
        cid = int(sr["id"]) if sr else next_session
        if not sr:
            next_session += 1
        sess_key_to_cid[key] = cid

    s_msgs = {r["_key"]: r for r in sc["messages"] if _alive(r, r["_key"], dead_messages, _message_ts)}
    c_msgs = {r["_key"]: r for r in cc["messages"] if _alive(r, r["_key"], dead_messages, _message_ts)}
    msg_keys = sorted(set(s_msgs) | set(c_msgs))
    next_msg = max([int(r["id"]) for r in s_msgs.values()] or [0]) + 1
    msg_key_to_cid: dict[str, int] = {}
    cmsg: dict[int, int] = {}
    for key in msg_keys:
        sr, cr = s_msgs.get(key), c_msgs.get(key)
        cid = int(sr["id"]) if sr else next_msg
        if not sr:
            next_msg += 1
        msg_key_to_cid[key] = cid
        if cr:
            cmsg[int(cr["id"])] = cid

    for idx in client_source_rows:
        out_sources[idx]["source_json"] = _remap_source_json(out_sources[idx]["source_json"], cmsg)

    out_sessions = []
    for key in session_keys:
        chosen, _ = _choose(s_sessions.get(key), c_sessions.get(key), _session_ts)
        participants = chosen["participants"]
        if key in s_sessions and key in c_sessions:
            participants = list_union_json(s_sessions[key]["participants"], c_sessions[key]["participants"])
        out_sessions.append({"id": sess_key_to_cid[key], "session_id": key, "platform": chosen["platform"],
                             "created_at": chosen["created_at"], "last_active_at": chosen["last_active_at"],
                             "message_count": 0, "participants": participants, "metadata": chosen["metadata"]})

    out_messages = []
    for key in msg_keys:
        chosen, _ = _choose(s_msgs.get(key), c_msgs.get(key), _message_ts)
        out_messages.append({"id": msg_key_to_cid[key], "session_id": chosen["session_id"],
                             "role": chosen["role"], "content": chosen["content"],
                             "sender_id": chosen["sender_id"], "sender_name": chosen["sender_name"],
                             "group_id": chosen["group_id"], "platform": chosen["platform"],
                             "timestamp": chosen["timestamp"], "metadata": chosen["metadata"], "_key": key})

    counts = {}
    for s in out_sessions:
        s["message_count"] = sum(1 for m in out_messages if m["session_id"] == s["session_id"])
    for table, data in (("documents", out_docs), ("atoms", out_atoms), ("sources", out_sources),
                        ("nodes", out_nodes), ("edges", out_edges), ("entries", out_entries),
                        ("entry_nodes", out_entry_nodes), ("graph_documents", out_gdocs),
                        ("sessions", out_sessions), ("messages", out_messages)):
        counts[table] = len(data)

    return {
        "schema": SCHEMA,
        "generated_at": now_ts(),
        "counts": counts,
        "memory": {"documents": out_docs, "atoms": out_atoms, "sources": out_sources,
                   "nodes": out_nodes, "edges": out_edges, "entries": out_entries,
                   "entry_nodes": out_entry_nodes, "graph_documents": out_gdocs,
                   "vectors": vectors, "graph_vectors": graph_vectors},
        "conversations": {"sessions": out_sessions, "messages": out_messages},
        "astrbot": merge_astrbot(server.get("astrbot"), client.get("astrbot"), dead),
        "deleted": {},
    }
