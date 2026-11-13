"""Shared helpers for the memory sync plugin."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime
from typing import Any

import numpy as np

SCHEMA = 1
DEFAULT_DIM = 1024

LM_DOC_FIELDS = ["id", "doc_id", "text", "metadata", "created_at", "updated_at"]
ATOM_FIELDS = [
    "id", "parent", "atom_type", "content", "entities", "importance", "confidence",
    "created_at", "last_accessed_at", "last_reinforced_at", "event_time", "ttl_days",
    "expires_at", "status", "reinforcement_count", "decay_type", "session_id",
    "persona_id", "metadata",
]
NODE_FIELDS = [
    "id", "node_key", "node_type", "node_value", "canonical_value", "metadata",
    "created_at", "updated_at",
]
EDGE_FIELDS = [
    "id", "edge_key", "source_node", "target_node", "relation_type", "source_doc",
    "weight", "confidence", "status", "metadata", "created_at", "updated_at",
]
ENTRY_FIELDS = [
    "id", "entry_key", "source_doc", "session_id", "persona_id", "entry_type",
    "relation_type", "content", "metadata", "edge_key", "vector_doc", "created_at",
    "updated_at",
]

TABLE_SORTS = {
    "documents": lambda r: r["doc_id"],
    "atoms": lambda r: r["_key"],
    "sources": lambda r: r["doc_id"],
    "nodes": lambda r: r["node_key"],
    "edges": lambda r: r["edge_key"],
    "entries": lambda r: r["entry_key"],
    "entry_nodes": lambda r: (r["entry_key"], r["node_key"]),
    "graph_documents": lambda r: r["doc_id"],
    "sessions": lambda r: r["session_id"],
    "messages": lambda r: r["_key"],
    "conversations": lambda r: r["conversation_id"],
    "personas": lambda r: r["persona_id"],
    "persona_folders": lambda r: r["folder_id"],
}


def now_ts() -> float:
    return time.time()


def to_ts(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_json(text: Any, default: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def meta_ts(raw: Any) -> float:
    meta = parse_json(raw, {})
    if not isinstance(meta, dict):
        return 0.0
    values = [meta.get("updated_at"), meta.get("update_time"), meta.get("last_access_time"), meta.get("create_time")]
    return max((to_ts(v) for v in values), default=0.0)


def list_union_json(raw_a: Any, raw_b: Any) -> str:
    values: list[Any] = []
    for raw in (raw_a, raw_b):
        data = parse_json(raw, [])
        if isinstance(data, list):
            for item in data:
                if item not in values:
                    values.append(item)
    return json.dumps(values, ensure_ascii=False)


def connect_ro(path: str) -> sqlite3.Connection:
    # A plain connection (not mode=ro) is used because SQLite may need to repair
    # or create the WAL shm file even for read-only work; mode=ro then reports
    # "disk I/O error" on Android. query_only keeps this connection read-only.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def rows(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql)]


def vector_map(index: Any, id_to_uuid: dict[int, str]) -> dict[str, str]:
    if index is None:
        return {}
    out: dict[str, str] = {}
    try:
        if hasattr(index, "id_map"):
            import faiss
            ids = faiss.vector_to_array(index.id_map).tolist()
        else:
            ids = list(range(index.ntotal))
    except Exception:
        ids = []
    inner = getattr(index, "index", None)
    for pos, raw_id in enumerate(ids):
        try:
            doc_id = id_to_uuid.get(int(raw_id))
            if not doc_id:
                continue
            if hasattr(index, "id_map") and inner is not None:
                vector = inner.reconstruct(pos)
            else:
                vector = index.reconstruct(int(raw_id))
        except Exception:
            continue
        arr = np.asarray(vector, dtype="<f4").reshape(-1)
        out[doc_id] = base64.b64encode(arr.tobytes()).decode("ascii")
    return out


def decode_vector(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype="<f4").astype("float32")


def normalized(snap: dict[str, Any]) -> dict[str, Any]:
    memory = snap.get("memory", {})
    conv = snap.get("conversations", {})
    out = {"schema": snap.get("schema", SCHEMA), "memory": {}, "conversations": {}}
    for table in ("documents", "atoms", "sources", "nodes", "edges", "entries", "entry_nodes", "graph_documents"):
        data = list(memory.get(table, []))
        data.sort(key=TABLE_SORTS[table])
        out["memory"][table] = data
    out["memory"]["vectors"] = {k: memory.get("vectors", {})[k] for k in sorted(memory.get("vectors", {}))}
    out["memory"]["graph_vectors"] = {k: memory.get("graph_vectors", {})[k] for k in sorted(memory.get("graph_vectors", {}))}
    for table in ("sessions", "messages"):
        data = list(conv.get(table, []))
        data.sort(key=TABLE_SORTS[table])
        out["conversations"][table] = data
    astro = snap.get("astrbot", {})
    out["astrbot"] = {}
    for table in ("conversations", "personas", "persona_folders"):
        data = list(astro.get(table, []))
        data.sort(key=TABLE_SORTS[table])
        out["astrbot"][table] = data
    return out


def snapshot_hash(snap: dict[str, Any]) -> str:
    return hashlib.sha256(json_dumps(normalized(snap)).encode("utf-8")).hexdigest()


def snapshot_counts(snap: dict[str, Any]) -> dict[str, int]:
    memory = snap.get("memory", {})
    conv = snap.get("conversations", {})
    return {
        "documents": len(memory.get("documents", [])),
        "atoms": len(memory.get("atoms", [])),
        "sources": len(memory.get("sources", [])),
        "nodes": len(memory.get("nodes", [])),
        "edges": len(memory.get("edges", [])),
        "entries": len(memory.get("entries", [])),
        "graph_documents": len(memory.get("graph_documents", [])),
        "sessions": len(conv.get("sessions", [])),
        "messages": len(conv.get("messages", [])),
        "vectors": len(memory.get("vectors", {})),
        "graph_vectors": len(memory.get("graph_vectors", {})),
        "astrbot_conversations": len(snap.get("astrbot", {}).get("conversations", [])),
        "astrbot_personas": len(snap.get("astrbot", {}).get("personas", [])),
        "astrbot_persona_folders": len(snap.get("astrbot", {}).get("persona_folders", [])),
    }


def ascii_temp(suffix: str = ".faiss") -> str:
    base = tempfile.gettempdir()
    if os.name == "nt" and not base.isascii():
        base = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")
    fd, path = tempfile.mkstemp(prefix="_memsync_", suffix=suffix, dir=base)
    os.close(fd)
    return path


def read_index_file(path: str) -> Any:
    import faiss
    if not os.path.isfile(path):
        return None
    try:
        return faiss.read_index(path)
    except RuntimeError:
        if os.name != "nt" or path.isascii():
            raise
        tmp = ascii_temp()
        try:
            shutil.copy2(path, tmp)
            return faiss.read_index(tmp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def write_index_file(index: Any, path: str) -> None:
    import faiss
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.name == "nt" and not path.isascii():
        tmp = ascii_temp()
        try:
            faiss.write_index(index, tmp)
            shutil.move(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return
    tmp = path + ".tmp"
    faiss.write_index(index, tmp)
    os.replace(tmp, path)
