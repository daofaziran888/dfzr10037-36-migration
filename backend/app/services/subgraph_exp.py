"""子图探查领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.reco_engine import ingest_runtime_rule, parse_count
from app.store import store


class SubgraphExpService(BaseModuleService):
    MODULE = "subgraph_exp"
    ENTITY = "探查配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["配置编号", "探查种子", "扩展策略", "关系过滤", "深度限制", "结果数量", "缓存策略", "上次探查"]
    REQUIRED_FIELDS = ["配置编号", "探查种子", "扩展策略"]
    STATUS_ORDER = ["草稿", "探查中", "已缓存", "已丢弃"]
    ACTION_RULES = {"启动探查": "探查中", "缓存结果": "已缓存", "丢弃缓存": "已丢弃"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        # 链路 L24: subgraph_exp → graph_search（探查结果更新搜索模板命中）
        if entry.get("status") in ("探查中", "已缓存"):
            search_msg = self._push_to_graph_search(entry)
            message += "；" + search_msg
        # 链路 L25: subgraph_exp → reco_engine（探查种子触发推荐）
        if entry.get("status") in ("探查中", "已缓存"):
            reco_msg = self._push_to_reco_engine(entry)
            message += "；" + reco_msg
        return entry, message

    def _push_to_graph_search(self, entry: dict[str, Any]) -> str:
        rows = store.rows("graph_search")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        gs_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "模板编号": f"GRAP-SUB-{next_id:04d}",
            "搜索类型": "subgraph-traversal",
            "查询语法": str(entry.get("扩展策略", "bfs")),
            "起始实体": str(entry.get("探查种子", "")),
            "关系过滤": str(entry.get("关系过滤", "")),
            "跳数上限": str(entry.get("深度限制", "3")),
            "排序规则": "relevance-desc",
            "使用频次": "0",
        }
        rows.append(gs_entry)
        return f"已生成搜索模板 {gs_entry['模板编号']}"

    def _push_to_reco_engine(self, entry: dict[str, Any]) -> str:
        # 链路 L25：探查带入的候选数量，来源 probe；实际证据数量=结果数量。
        raw_results = entry.get("结果数量", 10)
        results = parse_count(raw_results)
        reco_entry = ingest_runtime_rule(
            {
                "推荐场景": "子图探查关联推荐",
                "源实体": str(entry.get("探查种子", "")),
                "推荐算法": "subgraph-neighbor-rank",
                "候选数量": results if results is not None else raw_results,
                "过滤条件": str(entry.get("关系过滤", "")),
                "命中次数": "0",
                "采纳率": "0%",
            },
            source="probe",
            evidence=results if results is not None else 0,
            code_prefix="RECO-SUB",
        )
        return f"探查种子推送推荐引擎 {reco_entry['规则编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["graph_search", "reco_engine"]
        return base