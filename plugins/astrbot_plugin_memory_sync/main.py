"""AstrBot 双端长期记忆同步插件。

- 电脑端配置 role=server，手机端 role=client；同一份代码。
- 客户端定时把本地快照 POST 给服务端，服务端做确定性并集合并、落盘，再把
  规范快照返回给客户端落盘。两边以「自然键 + 服务端权威 ID」为准，合并后
  记忆、原子、图节点/边/关系、FAISS 向量、活跃会话完全一致。
- 删除通过上次同步基线差集形成墓碑，删除在两端都生效。
"""

from __future__ import annotations

import asyncio
import gzip
import hmac
import json
import os
import shutil
import sqlite3
import time
import traceback
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import sp
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.star import star_map
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from sqlalchemy import text

from .apply import apply_conversations_live, apply_snapshot, build_indexes
from .common import now_ts, read_index_file, snapshot_counts, snapshot_hash, to_ts, write_index_file
from .merge import merge_snapshots
from .snapshot import export_snapshot

PLUGIN_NAME = "astrbot_plugin_memory_sync"
LIVINGMEMORY_NAME = "astrbot_plugin_livingmemory"
SYNC_FILES = ("livingmemory.db", "livingmemory.index", "livingmemory_graph_documents.db",
              "livingmemory_graph.index", "conversations.db")


def _keys_by_table(snap: dict[str, Any]) -> dict[str, list[str]]:
    memory = snap.get("memory", {})
    conv = snap.get("conversations", {})
    return {
        "documents": [r["doc_id"] for r in memory.get("documents", [])],
        "atoms": [r["_key"] for r in memory.get("atoms", [])],
        "sources": [r["doc_id"] for r in memory.get("sources", [])],
        "nodes": [r["node_key"] for r in memory.get("nodes", [])],
        "edges": [r["edge_key"] for r in memory.get("edges", [])],
        "entries": [r["entry_key"] for r in memory.get("entries", [])],
        "graph_documents": [r["doc_id"] for r in memory.get("graph_documents", [])],
        "sessions": [r["session_id"] for r in conv.get("sessions", [])],
        "messages": [r["_key"] for r in conv.get("messages", [])],
        "astrbot_conversations": [r["conversation_id"] for r in snap.get("astrbot", {}).get("conversations", [])],
        "astrbot_personas": [r["persona_id"] for r in snap.get("astrbot", {}).get("personas", [])],
        "astrbot_persona_folders": [r["folder_id"] for r in snap.get("astrbot", {}).get("persona_folders", [])],
    }


class BaselineStore:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._init()

    def _init(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS baseline(table_name TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(table_name,key))")
            conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            conn.commit()
        finally:
            conn.close()

    def deleted_since(self, current: dict[str, list[str]]) -> dict[str, list[str]]:
        conn = sqlite3.connect(self.path)
        try:
            out: dict[str, list[str]] = {}
            for table, keys in current.items():
                old = {row[0] for row in conn.execute("SELECT key FROM baseline WHERE table_name=?", (table,))}
                missing = sorted(old - set(keys))
                if missing:
                    out[table] = missing
            return out
        finally:
            conn.close()

    def update(self, current: dict[str, list[str]]) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM baseline")
            rows = [(table, key) for table, keys in current.items() for key in keys]
            conn.executemany("INSERT OR IGNORE INTO baseline(table_name,key) VALUES(?,?)", rows)
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('updated_at',?)", (str(now_ts()),))
            conn.commit()
        finally:
            conn.close()

    def meta(self) -> dict[str, str]:
        conn = sqlite3.connect(self.path)
        try:
            return {k: v for k, v in conn.execute("SELECT k,v FROM meta")}
        finally:
            conn.close()


class MemorySyncPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.conf = config or {}
        self.data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        os.makedirs(self.data_dir, exist_ok=True)
        self.baseline = BaselineStore(os.path.join(self.data_dir, "sync_state.db"))
        self._sync_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._runner: web.AppRunner | None = None
        self._last: dict[str, Any] = {}
        self._lm = None
        self._wal_ready = False

    # ---------------------------------------------------------------- config

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.conf.get(key)
        except Exception:
            return default
        return default if value is None else value

    @property
    def role(self) -> str:
        return str(self._cfg("role", "client")).strip().lower()

    @property
    def token(self) -> str:
        return str(self._cfg("token", "")).strip()

    @property
    def is_server(self) -> bool:
        return self.role in ("server", "both")

    @property
    def is_client(self) -> bool:
        return self.role in ("client", "both") and bool(str(self._cfg("peer_url", "")).strip())

    # ---------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        if not bool(self._cfg("enabled", True)):
            logger.info("[memory-sync] 插件已禁用")
            return
        if not self.token:
            logger.warning("[memory-sync] 未配置 token，同步不会启动（防止局域网内被任意读写）")
            return
        if self.is_server:
            await self._start_server()
        interval = int(self._cfg("interval_minutes", 10))
        auto = self.is_client and interval > 0
        if auto and bool(self._cfg("sync_on_start", True)):
            asyncio.create_task(self._delayed_first_sync())
        if auto:
            self._task = asyncio.create_task(self._loop(interval))
        logger.info(f"[memory-sync] 已启动 role={self.role} auto={'on' if auto else 'off'} "
                    f"interval={interval}min peer={self._cfg('peer_url','')}")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def _delayed_first_sync(self) -> None:
        await asyncio.sleep(45)
        await self.sync_once("startup")

    async def _loop(self, minutes: int) -> None:
        if minutes <= 0:
            logger.info("[memory-sync] 自动同步已关闭，仅手动触发")
            return
        while True:
            try:
                await asyncio.sleep(max(60, minutes * 60))
                if self.is_client:
                    await self.sync_once("auto")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memory-sync] 定时同步失败: {exc}")

    # ---------------------------------------------------------------- livingmemory

    def _find_lm(self):
        if self._lm is not None:
            return self._lm
        for meta in list(star_map.values()):
            if (meta.name or "") == LIVINGMEMORY_NAME and meta.star_cls is not None:
                self._lm = meta.star_cls
                return self._lm
        return None

    async def _wait_ready(self, timeout: float = 240.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            lm = self._find_lm()
            init = getattr(lm, "initializer", None) if lm else None
            if init and getattr(init, "memory_engine", None) and getattr(init, "db", None) and getattr(init, "conversation_manager", None):
                return lm
            await asyncio.sleep(1.0)
        raise RuntimeError("LivingMemory 尚未初始化完成")

    def _lm_dir(self, lm) -> str:
        return str(lm.initializer.data_dir)

    def _ensure_wal(self, lm) -> None:
        if self._wal_ready:
            return
        data_dir = self._lm_dir(lm)
        for name in ("livingmemory_graph_documents.db",):
            path = os.path.join(data_dir, name)
            try:
                conn = sqlite3.connect(path, timeout=10)
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memory-sync] 切换 {name} 到 WAL 失败: {exc}")
        self._wal_ready = True

    async def _read_conversations(self, lm) -> dict[str, list[dict[str, Any]]]:
        store = lm.initializer.conversation_manager.store
        conn = store.connection
        if conn is None:
            raise RuntimeError("ConversationStore 连接未初始化")

        async def fetch(sql: str) -> list[dict[str, Any]]:
            cursor = await conn.execute(sql)
            data = await cursor.fetchall()
            names = [d[0] for d in (cursor.description or [])]
            await cursor.close()
            out = []
            for row in data:
                out.append(dict(row) if isinstance(row, sqlite3.Row) else dict(zip(names, row)))
            return out

        return {"sessions": await fetch("SELECT * FROM sessions"),
                "messages": await fetch("SELECT * FROM messages")}

    async def _export_live(self, lm, deleted: dict[str, list[str]] | None = None) -> dict[str, Any]:
        data_dir = self._lm_dir(lm)
        db = lm.initializer.db
        graph_db = lm.initializer.graph_db
        lm_index = db.embedding_storage.index if db is not None else read_index_file(os.path.join(data_dir, "livingmemory.index"))
        graph_index = graph_db.embedding_storage.index if graph_db is not None else read_index_file(os.path.join(data_dir, "livingmemory_graph.index"))
        conv_rows = await self._read_conversations(lm)
        astrbot_rows = await self._read_astrbot()
        return export_snapshot(
            os.path.join(data_dir, "livingmemory.db"),
            os.path.join(data_dir, "livingmemory_graph_documents.db"),
            os.path.join(data_dir, "conversations.db"),
            lm_index, graph_index, str(self._cfg("device_name", "astrbot")), deleted,
            conversation_rows=conv_rows, astrbot_rows=astrbot_rows,
        )

    async def _read_astrbot(self) -> dict[str, Any]:
        db = self.context.conversation_manager.db
        async with db.engine.connect() as conn:
            conv = (await conn.execute(text(
                "SELECT inner_conversation_id AS inner_id, conversation_id, platform_id, user_id, "
                "content, title, persona_id, token_usage, created_at, updated_at FROM conversations"))).mappings().all()
            pers = (await conn.execute(text(
                "SELECT id, persona_id, system_prompt, begin_dialogs, tools, skills, "
                "custom_error_message, folder_id, sort_order, created_at, updated_at FROM personas"))).mappings().all()
            folders = (await conn.execute(text(
                "SELECT id, folder_id, name, parent_id, description, sort_order, created_at, updated_at "
                "FROM persona_folders"))).mappings().all()

        def stamp(value):
            return value.isoformat(sep=" ") if hasattr(value, "isoformat") else value

        def content(value):
            try:
                return json.loads(value) if value else []
            except Exception:
                return []

        return {
            "conversations": [{
                "inner_id": int(r["inner_id"]), "conversation_id": r["conversation_id"],
                "platform_id": r["platform_id"], "user_id": r["user_id"],
                "content": content(r["content"]), "title": r["title"],
                "persona_id": r["persona_id"], "token_usage": int(r["token_usage"] or 0),
                "created_at": stamp(r["created_at"]), "updated_at": stamp(r["updated_at"]),
            } for r in conv],
            "personas": [dict(dict(r), created_at=stamp(r["created_at"]), updated_at=stamp(r["updated_at"]))
                         for r in pers],
            "persona_folders": [dict(dict(r), created_at=stamp(r["created_at"]), updated_at=stamp(r["updated_at"]))
                                for r in folders],
        }

    async def _apply_astrbot(self, data: dict[str, Any]) -> None:
        convs = data.get("conversations") or []
        personas = data.get("personas") or []
        folders = data.get("persona_folders") or []
        db = self.context.conversation_manager.db
        conv_rows = [{
            "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
            "inner_id": int(r["inner_id"]), "conversation_id": r["conversation_id"],
            "platform_id": r.get("platform_id"), "user_id": r.get("user_id"),
            "content": json.dumps(r.get("content") or [], ensure_ascii=False),
            "title": r.get("title"), "persona_id": r.get("persona_id"),
            "token_usage": int(r.get("token_usage") or 0),
        } for r in convs]
        persona_rows = []
        for r in personas:
            row = dict(r)
            for key in ("begin_dialogs", "tools", "skills"):
                value = row.get(key)
                if isinstance(value, (dict, list)):
                    row[key] = json.dumps(value, ensure_ascii=False)
            persona_rows.append(row)
        folder_rows = [dict(r) for r in folders]

        async with db.engine.begin() as conn:
            await conn.execute(text("DELETE FROM conversations"))
            if conv_rows:
                await conn.execute(text(
                    "INSERT INTO conversations(created_at,updated_at,inner_conversation_id,conversation_id,platform_id,"
                    "user_id,content,title,persona_id,token_usage) VALUES(:created_at,:updated_at,:inner_id,"
                    ":conversation_id,:platform_id,:user_id,:content,:title,:persona_id,:token_usage)"), conv_rows)
            await conn.execute(text("DELETE FROM personas"))
            if persona_rows:
                await conn.execute(text(
                    "INSERT INTO personas(created_at,updated_at,id,persona_id,system_prompt,begin_dialogs,tools,"
                    "skills,custom_error_message,folder_id,sort_order) VALUES(:created_at,:updated_at,:id,:persona_id,"
                    ":system_prompt,:begin_dialogs,:tools,:skills,:custom_error_message,:folder_id,:sort_order)"), persona_rows)
            await conn.execute(text("DELETE FROM persona_folders"))
            if folder_rows:
                await conn.execute(text(
                    "INSERT INTO persona_folders(created_at,updated_at,id,folder_id,name,parent_id,description,"
                    "sort_order) VALUES(:created_at,:updated_at,:id,:folder_id,:name,:parent_id,:description,:sort_order)"),
                    folder_rows)
            for table, max_id in (("conversations", max((int(r["inner_id"]) for r in conv_rows), default=0)),
                                  ("personas", max((int(r["id"]) for r in persona_rows), default=0)),
                                  ("persona_folders", max((int(r["id"]) for r in folder_rows), default=0))):
                try:
                    await conn.execute(text("DELETE FROM sqlite_sequence WHERE name=:name"), {"name": table})
                    if max_id:
                        await conn.execute(text("INSERT INTO sqlite_sequence(name,seq) VALUES(:name,:seq)"),
                                           {"name": table, "seq": max_id})
                except Exception:
                    pass

        try:
            await self.context.persona_manager.initialize()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memory-sync] 重载人设失败: {exc}")
        await self._fix_conversation_selection(convs)

    async def _fix_conversation_selection(self, convs: list[dict[str, Any]]) -> None:
        if not convs:
            return
        manager = self.context.conversation_manager
        cids = {r["conversation_id"] for r in convs}
        latest: dict[str, tuple[float, str]] = {}
        for r in convs:
            umo = r.get("user_id")
            if not umo:
                continue
            ts = max(to_ts(r.get("updated_at")), to_ts(r.get("created_at")))
            if umo not in latest or ts >= latest[umo][0]:
                latest[umo] = (ts, r["conversation_id"])
        latest_cid = {umo: cid for umo, (_, cid) in latest.items()}
        for umo, cid in list(getattr(manager, "session_conversations", {}).items()):
            if cid in cids:
                continue
            new = latest_cid.get(umo)
            try:
                if new:
                    manager.session_conversations[umo] = new
                    await sp.session_put(umo, "sel_conv_id", new)
                else:
                    manager.session_conversations.pop(umo, None)
                    await sp.session_remove(umo, "sel_conv_id")
            except Exception:
                pass
        for umo, cid in latest_cid.items():
            if umo in getattr(manager, "session_conversations", {}):
                continue
            try:
                old = await sp.session_get(umo, "sel_conv_id", None)
                if old and old not in cids:
                    await sp.session_put(umo, "sel_conv_id", cid)
            except Exception:
                pass

    # ---------------------------------------------------------------- backup

    def _backup(self, lm) -> None:
        data_dir = self._lm_dir(lm)
        root = os.path.join(self.data_dir, "backups")
        dest = os.path.join(root, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(dest, exist_ok=True)
        for name in ("livingmemory.db", "livingmemory_graph_documents.db"):
            src = os.path.join(data_dir, name)
            if not os.path.isfile(src):
                continue
            try:
                source = sqlite3.connect(src, timeout=30)
                target = sqlite3.connect(os.path.join(dest, name))
                with target:
                    source.backup(target)
                target.close(); source.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memory-sync] 备份 {name} 失败: {exc}")
        for name in ("livingmemory.index", "livingmemory_graph.index"):
            src = os.path.join(data_dir, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(dest, name))
        try:
            main_db = os.path.join(os.fspath(get_astrbot_data_path()), "data_v4.db")
            for suffix in ("", "-wal", "-shm"):
                src = main_db + suffix
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(dest, "data_v4.db" + suffix))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memory-sync] 备份 AstrBot 主库失败: {exc}")
        try:
            keep = max(1, int(self._cfg("backup_keep", 3)))
            dirs = sorted((d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))), reverse=True)
            for old in dirs[keep:]:
                shutil.rmtree(os.path.join(root, old), ignore_errors=True)
        except Exception:
            pass

    # ---------------------------------------------------------------- apply

    def _persister_for(self, lm, storage):
        for persister in getattr(lm.initializer, "_index_persisters", []) or []:
            if getattr(persister, "_storage", None) is storage:
                return persister
        return None

    async def _apply_live(self, lm, client_snap: dict[str, Any], merge_incoming: bool = True,
                          expected_local_hash: str | None = None) -> tuple[dict[str, Any], str]:
        """在 LivingMemory 运行中合并并落盘；返回规范快照与哈希。"""
        init = lm.initializer
        db, graph_db = init.db, init.graph_db
        data_dir = self._lm_dir(lm)
        lm_path = os.path.join(data_dir, "livingmemory.db")
        graph_path = os.path.join(data_dir, "livingmemory_graph_documents.db")
        p_lm = self._persister_for(lm, db.embedding_storage) if db is not None else None
        p_graph = self._persister_for(lm, graph_db.embedding_storage) if graph_db is not None else None

        await asyncio.to_thread(self._ensure_wal, lm)
        await asyncio.to_thread(self._backup, lm)
        conv_store = init.conversation_manager.store
        conv_lock = getattr(conv_store, "_write_lock", None)
        if conv_lock is not None:
            await conv_lock.acquire()
        old_lm_index = db.embedding_storage.index if db is not None else None
        old_graph_index = graph_db.embedding_storage.index if graph_db is not None else None
        conns: list[sqlite3.Connection] = []
        try:
            async with AsyncExitStack() as stack:
                for persister in (p_lm, p_graph):
                    if persister is not None:
                        await stack.enter_async_context(persister._index_lock.write())
                for path in (lm_path, graph_path):
                    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
                    conn.execute("PRAGMA busy_timeout=30000")
                    conns.append(conn)
                lm_conn, graph_conn = conns
                lm_conn.execute("BEGIN IMMEDIATE")
                graph_conn.execute("BEGIN IMMEDIATE")

                if merge_incoming:
                    server_snap = await self._export_live(lm)
                    server_snap["deleted"] = self.baseline.deleted_since(_keys_by_table(server_snap))
                    merged = merge_snapshots(server_snap, client_snap)
                else:
                    if expected_local_hash:
                        current = await self._export_live(lm)
                        if snapshot_hash(current) != expected_local_hash:
                            raise RuntimeError("本地在同步期间发生变化，稍后重试")
                    merged = client_snap
                apply_snapshot(lm_conn, graph_conn, None, merged)
                await apply_conversations_live(conv_store, merged["conversations"], lock_held=True)
                if merged.get("astrbot"):
                    await self._apply_astrbot(merged["astrbot"])
                new_lm_index, new_graph_index = build_indexes(merged)
                if db is not None:
                    db.embedding_storage.index = new_lm_index
                    db.document_storage._fts_index_ready = True
                if graph_db is not None:
                    graph_db.embedding_storage.index = new_graph_index
                    graph_db.document_storage._fts_index_ready = True
                lm_conn.commit(); graph_conn.commit()
        except Exception:
            for conn in conns:
                try:
                    conn.rollback()
                except Exception:
                    pass
            if db is not None:
                db.embedding_storage.index = old_lm_index
            if graph_db is not None:
                graph_db.embedding_storage.index = old_graph_index
            raise
        finally:
            for conn in conns:
                try:
                    conn.close()
                except Exception:
                    pass
            if conv_lock is not None and conv_lock.locked():
                conv_lock.release()

        index_jobs = []
        if db is not None:
            index_jobs.append((p_lm, db.embedding_storage, new_lm_index, os.path.join(data_dir, "livingmemory.index")))
        if graph_db is not None:
            index_jobs.append((p_graph, graph_db.embedding_storage, new_graph_index, os.path.join(data_dir, "livingmemory_graph.index")))
        for persister, storage, new_index, path in index_jobs:
            try:
                if persister is not None:
                    await persister.flush_now()
                else:
                    storage.index = new_index
                    await storage.save_index()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memory-sync] 索引异步落盘失败，改用直接写入: {exc}")
            try:
                disk = read_index_file(path)
                if disk is None or int(getattr(disk, "ntotal", -1)) != int(getattr(new_index, "ntotal", -2)):
                    write_index_file(new_index, path)
                    logger.info(f"[memory-sync] 已直接写入索引 {os.path.basename(path)} ntotal={getattr(new_index, 'ntotal', '?')}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[memory-sync] 索引文件校验/写入失败 {path}: {exc}")

        engine = init.memory_engine
        try:
            engine._invalidate_search_cache()
        except Exception:
            pass
        try:
            if getattr(engine, "graph_store", None) is not None:
                await engine.graph_store.close()
        except Exception:
            pass
        manager = init.conversation_manager
        try:
            async with manager._cache_lock:
                manager._cache.clear()
        except Exception:
            pass

        self.baseline.update(_keys_by_table(merged))
        return merged, snapshot_hash(merged)

    # ---------------------------------------------------------------- server

    async def _start_server(self) -> None:
        host = str(self._cfg("listen_host", "0.0.0.0"))
        port = int(self._cfg("listen_port", 8765))
        app = web.Application(client_max_size=512 * 1024 * 1024)
        app.router.add_post("/sync", self._handle_sync)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/state", self._handle_state)
        app.router.add_post("/sync-now", self._handle_sync_now)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._runner = runner
        logger.info(f"[memory-sync] 服务端监听 http://{host}:{port}/sync")

    def _authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get("X-Memory-Sync-Token", "")
        return bool(self.token) and hmac.compare_digest(supplied, self.token)

    async def _handle_health(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({
            "ok": True, "role": self.role, "last": self._last,
            "time": now_ts(),
        })

    async def _handle_state(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            lm = await self._wait_ready()
            snap = await self._export_live(lm)
            return web.json_response({"ok": True, "hash": snapshot_hash(snap),
                                      "counts": snapshot_counts(snap), "time": now_ts(),
                                      "data_dir": self._lm_dir(lm)})
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[memory-sync] state 失败: {exc}", exc_info=True)
            return web.json_response({"ok": False, "error": str(exc),
                                      "data_dir": self._lm_dir(lm) if 'lm' in dir() else None}, status=500)

    async def _handle_sync_now(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if not self.is_client:
            return web.json_response({"ok": False, "error": "当前不是客户端角色"}, status=400)
        try:
            result = await self.sync_once("http")
            return web.json_response({"ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _handle_sync(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            raw = await request.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            client_snap = json.loads(raw.decode("utf-8"))
            lm = await self._wait_ready()
            async with self._sync_lock:
                merged, digest = await self._apply_live(lm, client_snap)
            payload = json.dumps({"ok": True, "snapshot": merged, "hash": digest},
                                 ensure_ascii=False).encode("utf-8")
            self._last = {"time": now_ts(), "hash": digest, "counts": snapshot_counts(merged),
                          "source": "server", "device": client_snap.get("device")}
            logger.info(f"[memory-sync] 服务端合并完成 {snapshot_counts(merged)} hash={digest[:12]}")
            return web.Response(body=gzip.compress(payload, 5),
                                headers={"Content-Type": "application/json",
                                         "Content-Encoding": "gzip",
                                         "X-Memory-Sync-Hash": digest})
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[memory-sync] 服务端同步失败: {exc}", exc_info=True)
            try:
                with open(os.path.join(self.data_dir, "last_error.txt"), "w", encoding="utf-8") as fh:
                    fh.write(traceback.format_exc())
            except Exception:
                pass
            return web.json_response({"ok": False, "error": str(exc), "trace": traceback.format_exc()}, status=500)

    # ---------------------------------------------------------------- client

    async def _client_sync(self) -> dict[str, Any]:
        peer = str(self._cfg("peer_url", "")).strip().rstrip("/")
        if not peer:
            raise RuntimeError("未配置 peer_url")
        lm = await self._wait_ready()
        local = await self._export_live(lm)
        local["deleted"] = self.baseline.deleted_since(_keys_by_table(local))
        local_hash = snapshot_hash(local)
        body = gzip.compress(json.dumps(local, ensure_ascii=False).encode("utf-8"), 5)
        timeout = ClientTimeout(total=int(self._cfg("request_timeout", 600)))
        async with ClientSession(timeout=timeout) as session:
            if not local.get("deleted"):
                try:
                    async with session.get(peer + "/state",
                                           headers={"X-Memory-Sync-Token": self.token}) as state_resp:
                        if state_resp.status == 200:
                            state = await state_resp.json()
                            if state.get("hash") == local_hash:
                                self._last = {"time": now_ts(), "hash": local_hash,
                                              "counts": snapshot_counts(local), "source": "client",
                                              "peer": peer, "match": True, "skipped": True}
                                logger.info("[memory-sync] 两端已一致，本轮跳过上传与落盘")
                                return self._last
                except Exception:
                    pass
            async with session.post(peer + "/sync", data=body,
                                    headers={"Content-Type": "application/json",
                                             "Content-Encoding": "gzip",
                                             "X-Memory-Sync-Token": self.token}) as resp:
                raw = await resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}: {raw[:300]!r}")
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                result = json.loads(raw.decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "服务端返回失败")
        canonical = result["snapshot"]
        digest = result.get("hash") or snapshot_hash(canonical)

        # 网络期间本地有新消息/新记忆写入时，先不要覆盖，立即重启一轮。
        fresh = await self._export_live(lm)
        if snapshot_hash(fresh) != local_hash:
            raise RuntimeError("本地在同步期间发生变化，稍后重试")

        await self._apply_live(lm, canonical, merge_incoming=False, expected_local_hash=local_hash)
        after = await self._export_live(lm)
        after_hash = snapshot_hash(after)
        self._last = {"time": now_ts(), "hash": after_hash, "counts": snapshot_counts(after),
                      "source": "client", "peer": peer, "expected": digest,
                      "match": after_hash == digest}
        logger.info(f"[memory-sync] 客户端同步完成 {snapshot_counts(after)} "
                    f"hash={after_hash[:12]} match={after_hash == digest}")
        if after_hash != digest:
            raise RuntimeError(f"合并后哈希不一致 expected={digest[:12]} actual={after_hash[:12]}")
        return self._last

    async def sync_once(self, trigger: str = "manual") -> dict[str, Any]:
        async with self._sync_lock:
            for attempt in range(3):
                try:
                    result = await self._client_sync()
                    result["trigger"] = trigger
                    return result
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[memory-sync] 第 {attempt + 1} 次同步失败: {exc}", exc_info=(attempt == 0))
                    if attempt == 2:
                        self._last = {"time": now_ts(), "error": str(exc), "trigger": trigger}
                        raise
                    await asyncio.sleep(2)
        return self._last

    # ---------------------------------------------------------------- command

    @filter.command("记忆同步")
    async def memory_sync_command(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员可以操作记忆同步")
            return
        text = (event.message_str or "").strip()
        arg = text.split(maxsplit=1)[1].strip() if " " in text else ""
        if arg in ("状态", "status"):
            last = self._last or {}
            yield event.plain_result(
                f"记忆同步 role={self.role}\n"
                f"上次: {json.dumps(last, ensure_ascii=False)[:800]}\n"
                f"基线: {self.baseline.meta()}"
            )
            return
        if not self.is_client:
            yield event.plain_result("当前是服务端，手机端会自动来拉取；无需在电脑端主动同步")
            return
        try:
            result = await self.sync_once("manual")
            yield event.plain_result(
                f"同步完成 {json.dumps(result.get('counts', {}), ensure_ascii=False)}\n"
                f"hash={str(result.get('hash',''))[:16]} match={result.get('match')}"
            )
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"同步失败：{exc}")
