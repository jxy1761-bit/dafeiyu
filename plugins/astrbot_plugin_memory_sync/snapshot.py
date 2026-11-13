"""Export a namespace-free snapshot from the livingmemory data files."""

from __future__ import annotations

import json
from typing import Any

from .common import (
    SCHEMA, connect_ro, rows, vector_map, sha1, json_dumps, parse_json, now_ts,
)


def _message_key(m: dict[str, Any]) -> str:
    meta = parse_json(m["metadata"], {})
    return sha1(json_dumps([
        m["session_id"], m["role"], m["content"], m["sender_id"], m["sender_name"],
        m["group_id"], m["platform"], float(m["timestamp"] or 0), meta,
    ]))


def _build_conversations(sessions_raw: list[dict], messages_raw: list[dict]) -> dict[str, Any]:
    sessions = [{
        "id": int(s["id"]), "session_id": s["session_id"], "platform": s["platform"],
        "created_at": s["created_at"], "last_active_at": s["last_active_at"],
        "message_count": s["message_count"], "participants": s["participants"],
        "metadata": s["metadata"],
    } for s in sessions_raw]
    messages = []
    for m in messages_raw:
        messages.append({
            "id": int(m["id"]), "session_id": m["session_id"], "role": m["role"],
            "content": m["content"], "sender_id": m["sender_id"],
            "sender_name": m["sender_name"], "group_id": m["group_id"],
            "platform": m["platform"], "timestamp": m["timestamp"],
            "metadata": m["metadata"], "_key": _message_key(m),
        })
    return {"sessions": sessions, "messages": messages}


def export_snapshot(
    lm_db: str,
    graph_db: str,
    conv_db: str,
    lm_index: Any,
    graph_index: Any,
    device: str,
    deleted: dict[str, list[str]] | None = None,
    conversation_rows: dict[str, list[dict]] | None = None,
    astrbot_rows: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export a snapshot.

    ``conversation_rows`` lets the live plugin read sessions/messages through the
    ConversationStore's own connection; opening conversations.db separately can
    create a second WAL/shm view and break the live connection.
    """
    lm = connect_ro(lm_db)
    graph = connect_ro(graph_db)
    conv = None if conversation_rows is not None else connect_ro(conv_db)
    try:
        docs = rows(lm, "SELECT id,doc_id,text,metadata,created_at,updated_at FROM documents")
        doc_id_by_id = {int(r["id"]): r["doc_id"] for r in docs}
        gdocs = rows(graph, "SELECT id,doc_id,text,metadata,created_at,updated_at FROM documents")
        gdoc_id_by_id = {int(r["id"]): r["doc_id"] for r in gdocs}

        atoms: list[dict[str, Any]] = []
        for r in rows(lm, "SELECT * FROM memory_atoms"):
            parent = doc_id_by_id.get(int(r["parent_memory_id"]))
            if not parent:
                continue
            key = f"{parent}|{r['atom_type']}|{float(r['created_at'] or 0):.6f}|{sha1(str(r['content'] or ''))[:16]}"
            atoms.append({
                "id": int(r["id"]), "parent": parent, "atom_type": r["atom_type"],
                "content": r["content"], "entities": r["entities"],
                "importance": r["importance"], "confidence": r["confidence"],
                "created_at": r["created_at"], "last_accessed_at": r["last_accessed_at"],
                "last_reinforced_at": r["last_reinforced_at"], "event_time": r["event_time"],
                "ttl_days": r["ttl_days"], "expires_at": r["expires_at"], "status": r["status"],
                "reinforcement_count": r["reinforcement_count"], "decay_type": r["decay_type"],
                "session_id": r["session_id"], "persona_id": r["persona_id"],
                "metadata": r["metadata"], "_key": key,
            })

        sources = []
        for r in rows(lm, "SELECT * FROM memory_sources"):
            doc_id = doc_id_by_id.get(int(r["memory_id"]))
            if doc_id:
                sources.append({"doc_id": doc_id, "source_json": r["source_json"],
                                "created_at": r["created_at"], "updated_at": r["updated_at"]})

        nodes = rows(lm, "SELECT * FROM graph_nodes")
        node_key_by_id = {int(n["id"]): n["node_key"] for n in nodes}
        edge_key_by_id = {int(e["id"]): e["edge_key"] for e in rows(lm, "SELECT id,edge_key FROM graph_edges")}

        edges = []
        for e in rows(lm, "SELECT * FROM graph_edges"):
            src = node_key_by_id.get(int(e["source_node_id"]))
            dst = node_key_by_id.get(int(e["target_node_id"]))
            doc_id = doc_id_by_id.get(int(e["source_memory_id"]))
            if src and dst and doc_id:
                edges.append({
                    "id": int(e["id"]), "edge_key": e["edge_key"], "source_node": src,
                    "target_node": dst, "relation_type": e["relation_type"],
                    "source_doc": doc_id, "weight": e["weight"], "confidence": e["confidence"],
                    "status": e["status"], "metadata": e["metadata"],
                    "created_at": e["created_at"], "updated_at": e["updated_at"],
                })

        entries = []
        for e in rows(lm, "SELECT * FROM graph_entries"):
            doc_id = doc_id_by_id.get(int(e["source_memory_id"]))
            if not doc_id:
                continue
            entries.append({
                "id": int(e["id"]), "entry_key": e["entry_key"], "source_doc": doc_id,
                "session_id": e["session_id"], "persona_id": e["persona_id"],
                "entry_type": e["entry_type"], "relation_type": e["relation_type"],
                "content": e["content"], "metadata": e["metadata"],
                "edge_key": edge_key_by_id.get(int(e["edge_id"])) if e["edge_id"] else None,
                "vector_doc": gdoc_id_by_id.get(int(e["vector_doc_id"])) if e["vector_doc_id"] else None,
                "created_at": e["created_at"], "updated_at": e["updated_at"],
            })

        entry_key_by_id = {int(e["id"]): e["entry_key"] for e in rows(lm, "SELECT id,entry_key FROM graph_entries")}
        entry_nodes = []
        for r in rows(lm, "SELECT entry_id,node_id FROM graph_entry_nodes"):
            ek = entry_key_by_id.get(int(r["entry_id"]))
            nk = node_key_by_id.get(int(r["node_id"]))
            if ek and nk:
                entry_nodes.append({"entry_key": ek, "node_key": nk})

        if conversation_rows is not None:
            conversations = _build_conversations(conversation_rows.get("sessions", []),
                                                 conversation_rows.get("messages", []))
        else:
            conversations = _build_conversations(
                rows(conv, "SELECT * FROM sessions"),
                rows(conv, "SELECT * FROM messages"),
            )

        return {
            "schema": SCHEMA,
            "device": device,
            "generated_at": now_ts(),
            "memory": {
                "documents": docs, "atoms": atoms, "sources": sources,
                "nodes": nodes, "edges": edges, "entries": entries,
                "entry_nodes": entry_nodes, "graph_documents": gdocs,
                "vectors": vector_map(lm_index, doc_id_by_id),
                "graph_vectors": vector_map(graph_index, gdoc_id_by_id),
            },
            "conversations": conversations,
            "astrbot": astrbot_rows or {},
            "deleted": deleted or {},
        }
    finally:
        lm.close()
        graph.close()
        if conv is not None:
            conv.close()
