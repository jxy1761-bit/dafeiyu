"""记忆操作日志与按次撤销。

目的：任何整合 / 批量归档 / 批量更新 / dedup 合并，都能回答
「动了哪几条」并且可以按 op_id 还原，而不是只能靠整库备份。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from astrbot.api import logger

from ..utils.json_utils import safe_json_dict
from ..utils.number_utils import clamp_float


class MemoryEngineOpsMixin:
    """memory_ops 表 + 快照恢复 + 撤销。"""

    async def _ensure_ops_table(self, connection) -> None:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                op_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                actor TEXT,
                summary TEXT,
                payload_json TEXT NOT NULL,
                report_json TEXT,
                result_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                applied_at REAL,
                undone_at REAL
            )
            """
        )
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_ops_status "
            "ON memory_ops(status, id DESC)"
        )

    async def record_memory_op(
        self,
        op_type: str,
        payload: dict[str, Any],
        *,
        report: str = "",
        status: str = "pending",
        actor: str = "",
        summary: str = "",
    ) -> int:
        now = time.time()
        async with self._write_transaction() as connection:
            await self._ensure_ops_table(connection)
            cursor = await connection.execute(
                """
                INSERT INTO memory_ops (
                    op_type, status, actor, summary, payload_json, report_json,
                    result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(op_type),
                    str(status),
                    str(actor or "")[:120],
                    str(summary or "")[:500],
                    json.dumps(payload, ensure_ascii=False, default=str),
                    json.dumps({"text": report}, ensure_ascii=False, default=str),
                    json.dumps({}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            op_id = int(cursor.lastrowid or 0)
        # 只保留最近 500 条操作，避免无限增长
        try:
            async with self._write_transaction() as connection:
                await connection.execute(
                    """
                    DELETE FROM memory_ops
                    WHERE id NOT IN (
                        SELECT id FROM memory_ops ORDER BY id DESC LIMIT 500
                    )
                    """
                )
        except Exception:
            pass
        return op_id

    async def update_memory_op(
        self,
        op_id: int,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        report: str | None = None,
        summary: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if self.db_connection is None:
            return False
        fields: list[str] = ["updated_at = ?"]
        params: list[Any] = [time.time()]
        if payload is not None:
            fields.append("payload_json = ?")
            params.append(json.dumps(payload, ensure_ascii=False, default=str))
        if status is not None:
            fields.append("status = ?")
            params.append(str(status))
            if status == "applied":
                fields.append("applied_at = ?")
                params.append(time.time())
            elif status == "undone":
                fields.append("undone_at = ?")
                params.append(time.time())
        if result is not None:
            fields.append("result_json = ?")
            params.append(json.dumps(result, ensure_ascii=False, default=str))
        if report is not None:
            fields.append("report_json = ?")
            params.append(json.dumps({"text": report}, ensure_ascii=False, default=str))
        if summary is not None:
            fields.append("summary = ?")
            params.append(str(summary)[:500])
        params.append(int(op_id))
        async with self._write_transaction() as connection:
            await self._ensure_ops_table(connection)
            cursor = await connection.execute(
                f"UPDATE memory_ops SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            return int(cursor.rowcount or 0) > 0

    async def get_memory_op(self, op_id: int) -> dict[str, Any] | None:
        if self.db_connection is None:
            return None
        async with self._write_transaction() as connection:
            await self._ensure_ops_table(connection)
            cursor = await connection.execute(
                "SELECT * FROM memory_ops WHERE id = ?", (int(op_id),)
            )
            row = await cursor.fetchone()
        return self._op_row_to_dict(row) if row else None

    async def list_memory_ops(self, limit: int = 20) -> list[dict[str, Any]]:
        if self.db_connection is None:
            return []
        limit = max(1, min(100, int(limit or 20)))
        async with self._write_transaction() as connection:
            await self._ensure_ops_table(connection)
            cursor = await connection.execute(
                "SELECT * FROM memory_ops ORDER BY id DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
        return [self._op_row_to_dict(row) for row in rows]

    @staticmethod
    def _op_row_to_dict(row) -> dict[str, Any]:
        def _load(raw, default):
            try:
                value = json.loads(raw) if raw else default
            except (TypeError, ValueError):
                value = default
            return value

        return {
            "id": int(row["id"]),
            "op_type": row["op_type"],
            "status": row["status"],
            "actor": row["actor"],
            "summary": row["summary"],
            "payload": _load(row["payload_json"], {}),
            "report": _load(row["report_json"], {}).get("text", ""),
            "result": _load(row["result_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "applied_at": row["applied_at"],
            "undone_at": row["undone_at"],
        }

    async def snapshot_memory(self, memory_id: int) -> dict[str, Any] | None:
        """完整快照一条记忆（文档 + 来源消息）；不包含图/原子，恢复时重建。"""
        if self.db_connection is None:
            return None
        cursor = await self.db_connection.execute(
            "SELECT id, doc_id, text, metadata, created_at, updated_at "
            "FROM documents WHERE id = ?",
            (int(memory_id),),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        sources = []
        try:
            get_source = getattr(self, "get_memory_source", None)
            if callable(get_source):
                sources = await get_source(int(memory_id)) or []
        except Exception:
            sources = []
        return {
            "id": int(row["id"]),
            "doc_id": row["doc_id"],
            "text": row["text"],
            "metadata": safe_json_dict(row["metadata"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "sources": sources,
        }

    async def restore_memory_snapshot(self, snapshot: dict[str, Any]) -> bool:
        """把一条快照恢复成 active，并重建 FTS/向量/原子/图。"""
        if self.db_connection is None or not snapshot:
            return False
        memory_id = int(snapshot.get("id") or 0)
        text = str(snapshot.get("text") or "")
        metadata = dict(snapshot.get("metadata") or {})
        if not memory_id or not text.strip():
            return False

        async with self._write_transaction() as connection:
            cursor = await connection.execute(
                "SELECT id FROM documents WHERE id = ?", (memory_id,)
            )
            exists = await cursor.fetchone()
            if exists:
                await connection.execute(
                    "UPDATE documents SET text = ?, metadata = ? WHERE id = ?",
                    (
                        text,
                        json.dumps(
                            {**metadata, "status": "archived"}, ensure_ascii=False
                        ),
                        memory_id,
                    ),
                )
            else:
                doc_id = str(snapshot.get("doc_id") or f"restored-{memory_id}-{uuid.uuid4().hex}")
                cursor = await connection.execute(
                    "SELECT id FROM documents WHERE doc_id = ?", (doc_id,)
                )
                if await cursor.fetchone():
                    doc_id = f"restored-{memory_id}-{uuid.uuid4().hex}"
                await connection.execute(
                    "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        memory_id,
                        doc_id,
                        text,
                        json.dumps(
                            {**metadata, "status": "archived"}, ensure_ascii=False
                        ),
                        snapshot.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
                        snapshot.get("updated_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
            # 清掉旧索引/图/原子，避免恢复后重复
            await connection.execute(
                "DELETE FROM livingmemory_memories_fts WHERE doc_id = ?", (memory_id,)
            )

        embedding_storage = getattr(getattr(self, "faiss_db", None), "embedding_storage", None)
        embedding_delete = getattr(embedding_storage, "delete", None)
        if callable(embedding_delete):
            try:
                await embedding_delete([memory_id])
            except Exception:
                pass
        try:
            delete_graph_atoms = getattr(self, "_delete_graph_and_atoms_for_batch", None)
            if callable(delete_graph_atoms):
                await delete_graph_atoms([memory_id])
        except Exception:
            logger.warning("[MemoryOps] 恢复前清理图/原子失败", exc_info=True)

        try:
            restored = await self.restore_memory(memory_id)
        except Exception:
            logger.error("[MemoryOps] 重建记忆索引失败", exc_info=True)
            raise
        if not restored:
            return False

        # restore_memory 会写入 status=active/restored_at；这里覆盖回原始 metadata，
        # 但保留 active 状态，确保原档的 canonical_summary / topics 等不丢。
        final_metadata = dict(metadata)
        final_metadata["status"] = "active"
        final_metadata["restored_at"] = time.time()
        final_metadata.pop("archived_at", None)
        async with self._write_transaction() as connection:
            await connection.execute(
                "UPDATE documents SET metadata = ? WHERE id = ?",
                (json.dumps(final_metadata, ensure_ascii=False), memory_id),
            )
        try:
            save_source = getattr(self, "save_memory_source", None)
            if callable(save_source) and snapshot.get("sources"):
                await save_source(memory_id, snapshot["sources"])
        except Exception:
            logger.warning("[MemoryOps] 恢复来源消息失败", exc_info=True)
        self._invalidate_search_cache()
        return True

    async def undo_memory_op(self, op_id: int) -> dict[str, Any]:
        """按操作日志撤销一次记忆操作。"""
        op = await self.get_memory_op(op_id)
        if not op:
            return {"ok": False, "error": "operation not found", "op_id": op_id}
        if op["status"] == "undone":
            return {"ok": True, "already_undone": True, "op_id": op_id}
        if op["status"] == "pending":
            await self.update_memory_op(op_id, status="cancelled")
            return {"ok": True, "cancelled": True, "op_id": op_id}

        payload = op.get("payload") or {}
        restored = 0
        errors: list[str] = []
        op_type = str(op.get("op_type") or "")

        try:
            if op_type == "consolidation":
                result_groups = (op.get("result") or {}).get("groups") or []
                fallback_new_ids = [
                    int(item.get("new_id") or 0)
                    for item in result_groups
                    if isinstance(item, dict)
                ]
                for index, plan in enumerate(payload.get("plans") or []):
                    new_id = int(plan.get("new_id") or 0)
                    if not new_id and index < len(fallback_new_ids):
                        new_id = fallback_new_ids[index]
                    if new_id:
                        try:
                            current = await self.get_memory(new_id)
                            expected = str(plan.get("content") or "")
                            if current and expected and str(current.get("text") or "") != expected:
                                errors.append(
                                    f"新记忆 {new_id} 已被后续修改，拒绝直接删除"
                                )
                                continue
                            await self.delete_memory(new_id)
                        except Exception as exc:
                            errors.append(f"删除新记忆 {new_id} 失败: {exc}")
                    for snapshot in plan.get("snapshots") or []:
                        try:
                            if await self.restore_memory_snapshot(snapshot):
                                restored += 1
                        except Exception as exc:
                            errors.append(
                                f"恢复 #{snapshot.get('id')} 失败: {exc}"
                            )
                for item in payload.get("config_supersedes") or []:
                    snapshot = item.get("old_snapshot")
                    if not snapshot:
                        continue
                    try:
                        if await self.restore_memory_snapshot(snapshot):
                            restored += 1
                    except Exception as exc:
                        errors.append(
                            f"恢复过时配置 #{snapshot.get('id')} 失败: {exc}"
                        )
            elif op_type in {"batch_archive", "batch_delete", "batch_update", "dedup_merge"}:
                snapshots = payload.get("snapshots") or []
                if op_type == "dedup_merge" and payload.get("target_snapshot"):
                    snapshots = [payload["target_snapshot"]]
                for snapshot in snapshots:
                    try:
                        if await self.restore_memory_snapshot(snapshot):
                            restored += 1
                    except Exception as exc:
                        errors.append(f"恢复 #{snapshot.get('id')} 失败: {exc}")
            else:
                return {
                    "ok": False,
                    "error": f"unsupported operation type: {op_type}",
                    "op_id": op_id,
                }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[MemoryOps] 撤销失败 op=%s: %s", op_id, exc, exc_info=True)
            await self.update_memory_op(
                op_id,
                status="undo_failed",
                result={"restored": restored, "errors": [*errors, str(exc)]},
            )
            return {"ok": False, "error": str(exc), "op_id": op_id, "restored": restored}

        ok = not errors
        await self.update_memory_op(
            op_id,
            status="undone" if ok else "undo_partial",
            result={"restored": restored, "errors": errors},
        )
        return {
            "ok": ok,
            "op_id": op_id,
            "restored": restored,
            "errors": errors,
            "partial": bool(errors and restored),
        }
