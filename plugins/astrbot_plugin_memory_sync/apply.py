"""Apply a canonical snapshot to the three SQLite files and rebuild FAISS."""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from .common import DEFAULT_DIM, decode_vector, write_index_file

_LM_TABLES = ("graph_entry_nodes", "graph_entries", "graph_edges", "graph_nodes",
              "memory_atoms", "memory_sources", "documents")


def _stopwords() -> set[str]:
    try:
        from pathlib import Path
        import astrbot.core as core
        path = Path(core.__file__).parent / "knowledge_base" / "retrieval" / "hit_stopwords.txt"
        if path.is_file():
            return {w.strip() for w in path.read_text(encoding="utf-8").splitlines() if w.strip()}
    except Exception:
        pass
    return set()


def _fts_text(text: str, stopwords: set[str]) -> str:
    try:
        from astrbot.core.knowledge_base.retrieval.tokenizer import to_fts5_search_text
        return to_fts5_search_text(text or "", stopwords)
    except Exception:
        return text or ""


def _rebuild_doc_fts(conn: sqlite3.Connection, docs: list[dict[str, Any]], stopwords: set[str]) -> None:
    conn.execute("DROP TABLE IF EXISTS documents_fts")
    conn.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(search_text, content='', contentless_delete=1, tokenize='unicode61')")
    conn.executemany("INSERT INTO documents_fts(rowid, search_text) VALUES (?, ?)",
                     [(int(d["id"]), _fts_text(d["text"], stopwords)) for d in docs])


def _reset_sequence(conn: sqlite3.Connection, table: str, max_id: int) -> None:
    conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
    if max_id > 0:
        conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, max_id))


def _insert(conn: sqlite3.Connection, table: str, fields: list[str], data: list[dict[str, Any]]) -> None:
    if not data:
        return
    sql = f"INSERT INTO {table}({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})"
    conn.executemany(sql, [[row.get(f) for f in fields] for row in data])


def apply_snapshot(lm: sqlite3.Connection, graph: sqlite3.Connection, conv: sqlite3.Connection | None,
                   snap: dict[str, Any]) -> None:
    memory = snap["memory"]
    conversations = snap["conversations"]
    stopwords = _stopwords()

    for table in _LM_TABLES:
        lm.execute(f"DELETE FROM {table}")

    _insert(lm, "documents", ["id", "doc_id", "text", "metadata", "created_at", "updated_at"], memory["documents"])
    _doc_ids = {d["doc_id"]: int(d["id"]) for d in memory["documents"]}
    _node_ids = {n["node_key"]: int(n["id"]) for n in memory["nodes"]}
    _edge_ids = {e["edge_key"]: int(e["id"]) for e in memory["edges"]}
    _gdoc_ids = {d["doc_id"]: int(d["id"]) for d in memory["graph_documents"]}
    _insert(lm, "memory_atoms", [
        "id", "parent_memory_id", "atom_type", "content", "entities", "importance", "confidence",
        "created_at", "last_accessed_at", "last_reinforced_at", "event_time", "ttl_days",
        "expires_at", "status", "reinforcement_count", "decay_type", "session_id",
        "persona_id", "metadata"],
        [dict(a, parent_memory_id=_doc_ids[a["parent"]]) for a in memory["atoms"] if a["parent"] in _doc_ids])
    _insert(lm, "memory_sources", ["memory_id", "source_json", "created_at", "updated_at"],
            [{"memory_id": _doc_ids[r["doc_id"]], "source_json": r["source_json"],
              "created_at": r["created_at"], "updated_at": r["updated_at"]}
             for r in memory["sources"] if r["doc_id"] in _doc_ids])
    _insert(lm, "graph_nodes", ["id", "node_key", "node_type", "node_value", "canonical_value",
                                "metadata", "created_at", "updated_at"], memory["nodes"])
    _insert(lm, "graph_edges", ["id", "edge_key", "source_node_id", "target_node_id", "relation_type",
                                "source_memory_id", "weight", "confidence", "status", "metadata",
                                "created_at", "updated_at"],
            [dict(e, source_node_id=_node_ids[e["source_node"]], target_node_id=_node_ids[e["target_node"]],
                  source_memory_id=_doc_ids[e["source_doc"]])
             for e in memory["edges"] if e["source_node"] in _node_ids and e["target_node"] in _node_ids and e["source_doc"] in _doc_ids])
    _insert(lm, "graph_entries", ["id", "entry_key", "source_memory_id", "session_id", "persona_id",
                                  "entry_type", "relation_type", "content", "metadata", "edge_id",
                                  "vector_doc_id", "created_at", "updated_at"],
            [dict(e, source_memory_id=_doc_ids[e["source_doc"]],
                  edge_id=_edge_ids.get(e["edge_key"]),
                  vector_doc_id=_gdoc_ids.get(e["vector_doc"]))
             for e in memory["entries"] if e["source_doc"] in _doc_ids])
    _entry_ids = {e["entry_key"]: int(e["id"]) for e in memory["entries"]}
    _insert(lm, "graph_entry_nodes", ["entry_id", "node_id"], [
        {"entry_id": _entry_ids[r["entry_key"]], "node_id": _node_ids[r["node_key"]]}
        for r in memory["entry_nodes"] if r["entry_key"] in _entry_ids and r["node_key"] in _node_ids])

    _rebuild_doc_fts(lm, memory["documents"], stopwords)
    lm.execute("DELETE FROM livingmemory_memories_fts")
    lm.executemany("INSERT INTO livingmemory_memories_fts(content, doc_id) VALUES (?, ?)",
                   [(d["text"], d["doc_id"]) for d in memory["documents"]])
    lm.execute("DELETE FROM memory_atoms_fts")
    lm.executemany("INSERT INTO memory_atoms_fts(atom_id, content) VALUES (?, ?)",
                   [(a["id"], a["content"]) for a in memory["atoms"]])
    lm.execute("DELETE FROM livingmemory_graph_entries_fts")
    lm.executemany("INSERT INTO livingmemory_graph_entries_fts(entry_id, content) VALUES (?, ?)",
                   [(e["id"], e["content"]) for e in memory["entries"]])
    try:
        lm.execute("INSERT INTO livingmemory_graph_nodes_fts(livingmemory_graph_nodes_fts) VALUES('rebuild')")
    except Exception:
        pass

    for table, data in (("memory_atoms", memory["atoms"]), ("graph_nodes", memory["nodes"]),
                        ("graph_edges", memory["edges"]), ("graph_entries", memory["entries"])):
        _reset_sequence(lm, table, max((int(r["id"]) for r in data), default=0))

    graph.execute("DELETE FROM documents")
    _insert(graph, "documents", ["id", "doc_id", "text", "metadata", "created_at", "updated_at"],
            memory["graph_documents"])
    _rebuild_doc_fts(graph, memory["graph_documents"], stopwords)

    if conv is not None:
        conv.execute("DELETE FROM messages")
        conv.execute("DELETE FROM sessions")
        _insert(conv, "sessions", ["id", "session_id", "platform", "created_at", "last_active_at",
                                   "message_count", "participants", "metadata"], conversations["sessions"])
        _insert(conv, "messages", ["id", "session_id", "role", "content", "sender_id", "sender_name",
                                   "group_id", "platform", "timestamp", "metadata"], conversations["messages"])
        _reset_sequence(conv, "sessions", max((int(r["id"]) for r in conversations["sessions"]), default=0))
        _reset_sequence(conv, "messages", max((int(r["id"]) for r in conversations["messages"]), default=0))


def build_indexes(snap: dict[str, Any]) -> tuple[Any, Any]:
    import faiss
    import numpy as np

    memory = snap["memory"]
    doc_ids = {d["doc_id"]: int(d["id"]) for d in memory["documents"]}
    gdoc_ids = {d["doc_id"]: int(d["id"]) for d in memory["graph_documents"]}

    def build(items: dict[str, str], ids: dict[str, int]) -> Any:
        pairs = [(ids[k], decode_vector(v)) for k, v in items.items() if k in ids and v]
        dim = int(pairs[0][1].shape[0]) if pairs else DEFAULT_DIM
        index = faiss.IndexIDMap(faiss.IndexFlatL2(dim))
        if pairs:
            index.add_with_ids(np.vstack([v for _, v in pairs]),
                               np.asarray([i for i, _ in pairs], dtype=np.int64))
        return index

    return build(memory.get("vectors", {}), doc_ids), build(memory.get("graph_vectors", {}), gdoc_ids)


def apply_to_paths(data_dir: str, snap: dict[str, Any]) -> None:
    """Offline apply used by tests / repair tooling."""
    lm_path = os.path.join(data_dir, "livingmemory.db")
    graph_path = os.path.join(data_dir, "livingmemory_graph_documents.db")
    conv_path = os.path.join(data_dir, "conversations.db")
    lm = sqlite3.connect(lm_path, timeout=30)
    graph = sqlite3.connect(graph_path, timeout=30)
    conv = sqlite3.connect(conv_path, timeout=30)
    try:
        for conn in (lm, graph, conv):
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
        apply_snapshot(lm, graph, conv, snap)
        lm.commit(); graph.commit(); conv.commit()
    except Exception:
        lm.rollback(); graph.rollback(); conv.rollback()
        raise
    finally:
        lm.close(); graph.close(); conv.close()
    index, graph_index = build_indexes(snap)
    write_index_file(index, os.path.join(data_dir, "livingmemory.index"))
    write_index_file(graph_index, os.path.join(data_dir, "livingmemory_graph.index"))


async def apply_conversations_live(store: Any, conversations: dict[str, Any], lock_held: bool = False) -> None:
    """Replace sessions/messages through the live ConversationStore connection."""
    conn = store.connection
    if conn is None:
        raise RuntimeError("ConversationStore 连接未初始化")
    sessions = conversations.get("sessions", [])
    messages = conversations.get("messages", [])
    lock = getattr(store, "_write_lock", None)
    if lock is None:
        raise RuntimeError("ConversationStore 写锁不可用")
    async def _run():
        try:
            await conn.execute("BEGIN IMMEDIATE")
        except Exception:
            await conn.rollback()
            await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute("DELETE FROM messages")
            await conn.execute("DELETE FROM sessions")
            if sessions:
                await conn.executemany(
                    "INSERT INTO sessions(id,session_id,platform,created_at,last_active_at,message_count,participants,metadata) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    [(int(r["id"]), r["session_id"], r["platform"], r["created_at"], r["last_active_at"],
                      int(r["message_count"] or 0), r["participants"], r["metadata"]) for r in sessions],
                )
            if messages:
                await conn.executemany(
                    "INSERT INTO messages(id,session_id,role,content,sender_id,sender_name,group_id,platform,timestamp,metadata) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(int(r["id"]), r["session_id"], r["role"], r["content"], r["sender_id"],
                      r["sender_name"], r["group_id"], r["platform"], r["timestamp"], r["metadata"])
                     for r in messages],
                )
            await conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('sessions','messages')")
            if sessions:
                await conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('sessions',?)",
                                   (max(int(r["id"]) for r in sessions),))
            if messages:
                await conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('messages',?)",
                                   (max(int(r["id"]) for r in messages),))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    if lock_held:
        await _run()
    else:
        async with lock:
            await _run()
