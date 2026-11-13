"""记忆库定期整合管理器。

把零散的低价值记忆聚合、整理、总结为更精炼的记忆，从源头控制记忆库规模。
聚合粒度（同会话 / 语义聚类）、旧记忆处理（归档 / 删除）、触发方式（每日 / 反思）
均由 memory_consolidation 配置控制。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from astrbot.api import logger


class MemoryConsolidationManager:
    """按配置定期整合记忆库。"""

    def __init__(self, memory_engine, memory_processor, config_manager):
        self.memory_engine = memory_engine
        self.memory_processor = memory_processor
        self.config_manager = config_manager
        # reflection 触发模式下的最小运行间隔（秒），避免每条消息都触发
        self.write_lock = asyncio.Lock()
        self._last_run_at = 0.0
        self._min_run_interval = 6 * 3600.0

    @property
    def config(self) -> dict[str, Any]:
        return self.config_manager.get_section("memory_consolidation")

    async def maybe_run(self, trigger: str) -> dict[str, Any]:
        """按触发方式执行整合（不匹配或未启用时跳过）。

        Args:
            trigger: "daily" 或 "reflection"。
        """
        cfg = self.config
        if not cfg.get("enabled", False) or cfg.get("trigger", "daily") != trigger:
            return {"skipped": True}
        return await self.run_consolidation(force=(trigger == "daily"))

    async def run_consolidation(self, force: bool = False) -> dict[str, Any]:
        """执行一轮记忆整合。返回统计信息。

        Args:
            force: 是否忽略最小运行间隔（每日定时触发时传入 True）。
        """
        cfg = self.config
        if not cfg.get("enabled", False):
            return {"skipped": True}
        if not self.memory_engine or not self.memory_processor:
            logger.warning("[记忆整合] 组件未就绪，跳过")
            return {"skipped": True, "reason": "components not ready"}

        now = time.time()
        if not force and now - self._last_run_at < self._min_run_interval:
            return {"skipped": True, "reason": "cooldown"}
        self._last_run_at = now

        try:
            candidates = await self._query_candidates(cfg)
            if not candidates:
                return {"candidates": 0, "groups": 0, "merged": 0}

            groups = await self._build_groups(candidates, cfg)
            if not groups:
                return {"candidates": len(candidates), "groups": 0, "merged": 0}

            max_groups = int(cfg.get("max_groups_per_run", 5))
            keep_original = cfg.get("keep_original", "archive")
            stats = {
                "candidates": len(candidates),
                "groups": 0,
                "merged": 0,
                "archived": 0,
                "deleted": 0,
                "failed": 0,
            }
            for group in groups[:max_groups]:
                try:
                    result = await self._consolidate_group(group, cfg)
                    stats["groups"] += 1
                    stats["merged"] += result["merged"]
                    # 做梦强制归档旧档，保证可按 op_id 撤销；不再物理删除。
                    stats["archived"] += result["removed"]
                except Exception as e:
                    stats["failed"] += 1
                    logger.error(f"[记忆整合] 整合组失败: {e}", exc_info=True)

            logger.info(f"[记忆整合] 完成: {stats}")
            return stats
        except Exception as e:
            logger.error(f"[记忆整合] 运行失败: {e}", exc_info=True)
            return {"error": str(e)}

    async def _query_candidates(self, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """查询符合整合条件的候选记忆（低重要度 + 足够旧 + 活跃状态）。"""
        protected_threshold = float(
            self.config_manager.get(
                "importance_decay.protected_importance_threshold", 0.9
            )
            or 0.9
        )
        max_importance = min(
            float(cfg.get("max_importance", 0.5)), protected_threshold
        )
        cutoff = time.time() - int(cfg.get("min_age_days", 7)) * 86400.0
        try:
            return await self.memory_engine.get_consolidation_candidates(
                max_importance, cutoff
            )
        except Exception as e:
            logger.error(f"[记忆整合] 查询候选记忆失败: {e}", exc_info=True)
            return []

    async def _build_groups(
        self, candidates: list[dict[str, Any]], cfg: dict[str, Any]
    ) -> list[list[dict[str, Any]]]:
        granularity = cfg.get("granularity", "session")
        if granularity == "semantic":
            # 语义聚类可能跨会话：合并前必须按 (session_id, persona_id) 拆分，
            # 否则合并记忆要么无作用域（任何会话都召回不到），要么把 A 会话
            # 的内容写进 B 会话的作用域。
            semantic_groups = await self._group_semantic(candidates, cfg)
            groups = [
                scope_group
                for semantic_group in semantic_groups
                for scope_group in self._split_by_scope(semantic_group)
            ]
        else:
            groups = self._split_by_scope(candidates)

        min_per = int(cfg.get("min_memories_per_group", 3))
        groups = [g for g in groups if len(g) >= min_per]
        groups.sort(key=len, reverse=True)
        return groups

    @staticmethod
    def _split_by_scope(
        group: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        """Keep session, persona and adapter ownership separate during consolidation."""
        buckets: dict[tuple[str, Any, Any], list[dict[str, Any]]] = {}
        for mem in group:
            metadata = mem["metadata"]
            session_id = metadata.get("session_id")
            if not session_id:
                continue
            key = (
                str(session_id),
                metadata.get("persona_id"),
                metadata.get("adapter_id"),
            )
            buckets.setdefault(key, []).append(mem)
        return list(buckets.values())

    async def _group_semantic(
        self, candidates: list[dict[str, Any]], cfg: dict[str, Any]
    ) -> list[list[dict[str, Any]]]:
        """跨会话语义聚类：复用索引内向量批量查找相似对，再做连通分量合并。

        可扩展到上万条候选：不逐条调用 Embedding API，而是批量 reconstruct + 批量
        Faiss 搜索，内存峰值由 vector_retriever 的分块控制。
        """
        threshold = float(cfg.get("semantic_similarity_threshold", 0.7))
        candidate_ids = [mem["id"] for mem in candidates]

        pairs: list[tuple[int, int, float]] = []
        try:
            pairs = await self.memory_engine.find_similar_pairs(
                candidate_ids, threshold
            )
        except Exception as e:
            logger.warning(f"[记忆整合] 语义聚类失败，回退到同会话聚合: {e}")
            return self._split_by_scope(candidates)

        if not pairs:
            return []

        parent: dict[int, int] = {mem["id"]: mem["id"] for mem in candidates}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for a, b, _sim in pairs:
            union(a, b)

        groups: dict[int, list[dict[str, Any]]] = {}
        for mem in candidates:
            root = find(mem["id"])
            groups.setdefault(root, []).append(mem)
        return list(groups.values())

    def _guard(self, group: list[dict[str, Any]]) -> None:
        protected_threshold = float(
            self.config_manager.get(
                "importance_decay.protected_importance_threshold", 0.9
            )
            or 0.9
        )
        guarded = [
            mem
            for mem in group
            if float(mem.get("metadata", {}).get("importance", 0.5) or 0.5)
            >= protected_threshold
        ]
        if guarded:
            raise RuntimeError(
                "高分条目进入整合组，已拒绝（high-score append-only guard）"
            )

    async def _build_plan(
        self, group: list[dict[str, Any]], cfg: dict[str, Any]
    ) -> dict[str, Any]:
        self._guard(group)
        merged = await self.memory_processor.merge_memories(group)
        summary = merged["summary"]
        key_facts = merged["key_facts"]
        rich_content = summary
        if key_facts:
            rich_content = f"{summary} | {'；'.join(key_facts)}"

        session_id = group[0]["metadata"].get("session_id")
        persona_id = group[0]["metadata"].get("persona_id")
        if not session_id:
            raise RuntimeError("整合组缺少会话作用域，无法写出可召回的合并记忆")

        snapshots = []
        for mem in group:
            snapshot = await self.memory_engine.snapshot_memory(int(mem["id"]))
            if snapshot:
                snapshots.append(snapshot)

        metadata = {
            "adapter_id": group[0]["metadata"].get("adapter_id"),
            "topics": merged["topics"],
            "key_facts": key_facts,
            "persona_summary": summary,
            "canonical_summary": rich_content,
            "summary_schema_version": "v2",
            "consolidated_from": [mem["id"] for mem in group],
            "consolidated_at": time.time(),
        }
        return {
            "session_id": session_id,
            "persona_id": persona_id,
            "old_ids": [int(mem["id"]) for mem in group],
            "snapshots": snapshots,
            "content": rich_content,
            "summary": summary,
            "key_facts": key_facts,
            "topics": merged["topics"],
            "importance": merged["importance"],
            "metadata": metadata,
            # 梦境强制归档旧档，保证可撤销；配置里的 delete 不用于自动梦境。
            "keep_original": "archive",
        }

    async def _write_plan(self, plan: dict[str, Any], *, op_id: int | None = None) -> dict[str, Any]:
        async with self.write_lock:
            metadata = dict(plan.get("metadata") or {})
            if op_id:
                metadata["consolidated_op_id"] = op_id
            new_id = await self.memory_engine.add_memory(
                content=plan["content"],
                session_id=plan["session_id"],
                persona_id=plan.get("persona_id"),
                importance=float(plan.get("importance") or 0.5),
                metadata=metadata,
            )
            old_ids = [int(item) for item in plan.get("old_ids") or []]
            try:
                removed = await self.memory_engine.archive_memories(old_ids)
            except Exception:
                try:
                    await self.memory_engine.delete_memory(new_id)
                except Exception:
                    logger.error(
                        f"[记忆整合] 归档失败后回滚新记忆 {new_id} 也失败",
                        exc_info=True,
                    )
                raise
            plan["new_id"] = new_id
            plan["archived"] = removed
            logger.info(
                f"[记忆整合] 整合 {len(old_ids)} 条记忆 -> 新记忆 {new_id}，"
                f"归档 {removed} 条旧记忆"
            )
            return {"new_id": new_id, "merged": len(old_ids), "removed": removed}

    _CONFIG_KEYWORDS = (
        "配置", "参数", "设置", "规则", "约定", "日限", "每天", "每日",
        "上限", "最多", "限制", "间隔", "频率", "预算", "token", "额度",
    )
    _GENERIC_TOPICS = {"配置", "设置", "参数", "规则", "约定", "偏好", "限制", "记忆", "系统"}

    def _is_config_like(self, mem: dict[str, Any]) -> bool:
        md = mem.get("metadata") or {}
        text = str(mem.get("content") or "")
        blob = " ".join([text, " ".join(str(x) for x in (md.get("topics") or []))])
        return any(word in blob for word in self._CONFIG_KEYWORDS) and bool(
            re.search(r"\d+(?:\.\d+)?", text)
        )

    def _config_signature(self, mem: dict[str, Any]) -> tuple[str, ...] | None:
        md = mem.get("metadata") or {}
        topics = sorted(
            str(x).strip()
            for x in (md.get("topics") or [])
            if str(x).strip() and str(x).strip() not in self._GENERIC_TOPICS
        )
        if not topics:
            return None
        return tuple(topics)

    async def detect_config_supersedes(
        self, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """找同一主题下参数不同的一组配置记忆，旧档标过时。"""
        buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for mem in candidates:
            if not self._is_config_like(mem):
                continue
            sig = self._config_signature(mem)
            if sig:
                buckets.setdefault(sig, []).append(mem)
        found: list[dict[str, Any]] = []
        seen_old: set[int] = set()
        for sig, mems in buckets.items():
            if len(mems) < 2:
                continue
            mems = sorted(
                mems,
                key=lambda m: float((m.get("metadata") or {}).get("create_time") or 0),
            )
            newest = mems[-1]
            newest_numbers = set(re.findall(r"\d+(?:\.\d+)?", str(newest.get("content") or "")))
            if not newest_numbers:
                continue
            for old in mems[:-1]:
                old_id = int(old.get("id") or 0)
                if not old_id or old_id in seen_old:
                    continue
                old_numbers = set(re.findall(r"\d+(?:\.\d+)?", str(old.get("content") or "")))
                if old_numbers == newest_numbers:
                    continue
                snapshot = None
                try:
                    snapshot = await self.memory_engine.snapshot_memory(old_id)
                except Exception:
                    snapshot = None
                if not snapshot:
                    continue
                seen_old.add(old_id)
                found.append(
                    {
                        "old_id": old_id,
                        "new_id": int(newest.get("id") or 0),
                        "old_snapshot": snapshot,
                        "reason": (
                            f"配置参数更新：{sorted(old_numbers) or ['—']} -> "
                            f"{sorted(newest_numbers)}"
                        ),
                    }
                )
        return found

    @staticmethod
    def _preview_of(text: str, limit: int = 90) -> str:
        text = " ".join(str(text or "").split())
        return text[:limit] + ("…" if len(text) > limit else "")

    def format_plan_report(
        self,
        plans: list[dict[str, Any]],
        *,
        op_id: int | None = None,
        applied: bool = False,
        config_supersedes: list[dict[str, Any]] | None = None,
    ) -> str:
        title = "【做梦报告】" if applied else "【做梦预览】"
        total_items = sum(len(p.get('old_ids') or []) for p in plans)
        total_items += len(config_supersedes or [])
        lines = [f"{title}候选 {total_items} 条 / {len(plans)} 组"]
        for index, plan in enumerate(plans, 1):
            lines.append(f"\n组 {index}：{len(plan.get('old_ids') or [])} 条")
            for snapshot in plan.get("snapshots") or []:
                md = snapshot.get("metadata") or {}
                imp = md.get("importance")
                imp_text = f"{float(imp):.2f}" if imp is not None else "—"
                lines.append(
                    f"  #{snapshot.get('id')} [{imp_text}] {self._preview_of(snapshot.get('text'))}"
                )
            lines.append(f"  合并后：{self._preview_of(plan.get('content'), 220)}")
            lines.append("  原档处理：归档（可按 op_id 撤销）")
        if config_supersedes:
            lines.append("\n【配置过时】" + str(len(config_supersedes)) + " 条")
            for item in config_supersedes:
                snap = item.get("old_snapshot") or {}
                lines.append(
                    f"  #{item.get('old_id')} 标过时，保留 #{item.get('new_id')}："
                    f"{item.get('reason', '')}；{self._preview_of(snap.get('text'), 90)}"
                )
        protected = float(
            self.config_manager.get(
                "importance_decay.protected_importance_threshold", 0.9
            )
            or 0.9
        )
        lines.append(
            f"\n高分保护：importance >= {protected:.2f} 的条目不会进入候选，也不会被合并。"
        )
        if op_id:
            if applied:
                lines.append(f"已完成，撤销：memory_op_undo(op_id={op_id})")
            else:
                lines.append(
                    f"模型可自行审阅后执行：dream_apply(op_id={op_id})；"
                    f"撤销：memory_op_undo(op_id={op_id})"
                )
        return "\n".join(lines)

    async def preview_consolidation(
        self, max_groups: int | None = None
    ) -> dict[str, Any]:
        """只生成计划、不写记忆；返回 op_id 和逐条报告。"""
        cfg = self.config
        candidates = await self._query_candidates(cfg)
        config_supersedes: list[dict[str, Any]] = []
        if bool(
            self.config_manager.get("memory_dream.config_supersede_enabled", True)
        ):
            try:
                config_supersedes = await self.detect_config_supersedes(candidates)
            except Exception:
                logger.warning("[记忆整合] 配置冲突检测失败，跳过", exc_info=True)
        superseded_old_ids = {int(item["old_id"]) for item in config_supersedes}
        if superseded_old_ids:
            candidates = [
                mem for mem in candidates if int(mem["id"]) not in superseded_old_ids
            ]
        if not candidates and not config_supersedes:
            return {"op_id": None, "plans": [], "candidates": 0, "groups": 0,
                    "config_supersedes": [], "report": "没有可整合的低分旧记忆；高分条目不会被选中。"}
        groups = await self._build_groups(candidates, cfg) if candidates else []
        if not groups and not config_supersedes:
            return {"op_id": None, "plans": [], "candidates": len(candidates), "groups": 0,
                    "config_supersedes": [], "report": "没有达到最小条数的整合组。"}
        group_limit = max_groups if max_groups is not None else int(
            cfg.get("max_groups_per_run", 5)
        )
        group_limit = max(0, min(10, int(group_limit)))
        plans = []
        for group in groups[:group_limit]:
            try:
                plans.append(await self._build_plan(group, cfg))
            except Exception as exc:
                logger.warning("[记忆整合] 预览某组失败，已跳过: %s", exc)
        if not plans and not config_supersedes:
            return {"op_id": None, "plans": [], "candidates": len(candidates), "groups": len(groups),
                    "config_supersedes": [], "report": "候选组都无法生成合并摘要，本次不执行。"}
        # 先写 pending op，再生成包含 op_id 的报告
        total_items = sum(len(p.get('old_ids') or []) for p in plans) + len(config_supersedes)
        op_id = await self.memory_engine.record_memory_op(
            "consolidation",
            {"plans": plans, "config_supersedes": config_supersedes},
            report="",
            status="pending",
            actor="dream_preview",
            summary=f"做梦预览：{len(plans)} 组 / {total_items} 条",
        )
        report = self.format_plan_report(
            plans, op_id=op_id, config_supersedes=config_supersedes
        )
        await self.memory_engine.update_memory_op(op_id, report=report)
        return {
            "op_id": op_id,
            "plans": plans,
            "candidates": len(candidates),
            "groups": len(groups),
            "config_supersedes": config_supersedes,
            "report": report,
        }

    async def apply_consolidation(self, op_id: int) -> dict[str, Any]:
        """执行一次已预览的做梦计划。"""
        op = await self.memory_engine.get_memory_op(int(op_id))
        if not op:
            return {"ok": False, "error": "operation not found", "op_id": op_id}
        if op.get("op_type") != "consolidation":
            return {"ok": False, "error": "not a consolidation operation", "op_id": op_id}
        if op.get("status") != "pending":
            return {"ok": False, "error": f"status is {op.get('status')}, not pending", "op_id": op_id}
        payload = op.get("payload") or {}
        plans = payload.get("plans") or []
        config_supersedes = payload.get("config_supersedes") or []
        results = []
        config_results = []
        errors = []
        for plan in plans:
            try:
                results.append(await self._write_plan(plan, op_id=int(op_id)))
            except Exception as exc:
                errors.append(str(exc))
        for item in config_supersedes:
            try:
                old_id = int(item.get("old_id") or 0)
                new_id = int(item.get("new_id") or 0)
                reason = str(item.get("reason") or "配置参数更新")
                if not old_id:
                    continue
                await self.memory_engine.archive_memories(
                    [old_id],
                    audit={
                        "archived_by": "dream_config_supersede",
                        "outdated_by": new_id,
                        "outdated_reason": reason,
                    },
                )
                config_results.append(
                    {"old_id": old_id, "new_id": new_id, "reason": reason}
                )
            except Exception as exc:
                errors.append(str(exc))
        result = {
            "groups": results,
            "config_superseded": config_results,
            "errors": errors,
        }
        report = self.format_plan_report(
            plans,
            op_id=int(op_id),
            applied=not errors,
            config_supersedes=config_supersedes,
        )
        if errors:
            report += (
                f"\n执行结果：成功 {len(results)} 组 / "
                f"{len(config_results)} 条配置过时，失败 {len(errors)} 项。"
            )
        else:
            report += (
                f"\n执行结果：{len(results)} 组已合并，"
                f"{len(config_results)} 条配置旧档已标过时，原档已归档。"
            )
        await self.memory_engine.update_memory_op(
            int(op_id),
            status="applied" if not errors else "applied_partial",
            result=result,
            report=report,
            summary=(
                f"做梦执行：成功 {len(results)} 组 / "
                f"{len(config_results)} 条配置过时，失败 {len(errors)} 项"
            ),
            payload={"plans": plans, "config_supersedes": config_supersedes},
        )
        return {
            "ok": not errors,
            "op_id": int(op_id),
            "applied": len(results),
            "config_superseded": len(config_results),
            "errors": errors,
            "report": report,
        }

    async def _consolidate_group(
        self, group: list[dict[str, Any]], cfg: dict[str, Any]
    ) -> dict[str, Any]:
        plan = await self._build_plan(group, cfg)
        result = await self._write_plan(plan)
        try:
            op_id = await self.memory_engine.record_memory_op(
                "consolidation",
                {"plans": [plan]},
                report=self.format_plan_report([plan]),
                status="applied",
                actor="daily_consolidation",
                summary=f"自动做梦：{len(plan.get('old_ids') or [])} 条 -> #{result['new_id']}",
            )
            await self.memory_engine.update_memory_op(
                op_id,
                result=result,
                report=self.format_plan_report(
                    [plan], op_id=op_id, applied=True
                ),
            )
            result["op_id"] = op_id
        except Exception as exc:
            logger.warning("[记忆整合] 写操作日志失败（合并本身已成功）: %s", exc)
        return result


__all__ = ["MemoryConsolidationManager"]
