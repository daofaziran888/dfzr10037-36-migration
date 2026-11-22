"""路径推理领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.reco_engine import ingest_runtime_rule, parse_count
from app.store import store


class PathReasonService(BaseModuleService):
    MODULE = "path_reason"
    ENTITY = "推理任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "起始实体", "目标实体", "推理模式", "最大跳数", "路径数量", "置信阈值", "推理耗时"]
    REQUIRED_FIELDS = ["任务编号", "起始实体", "目标实体"]
    STATUS_ORDER = ["待推理", "推理中", "已完成", "推理超时"]
    ACTION_RULES = {"启动推理": "推理中", "取消推理": "已完成", "标记超时": "推理超时"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        # 链路 L10: path_reason → query_cache（推理结果写入查询缓存）
        if entry.get("status") == "已完成":
            cache_msg = self._push_to_query_cache(entry)
            message += "；" + cache_msg
        # 链路 L11: path_reason → reco_engine（推理路径推送推荐引擎）
        if entry.get("status") == "已完成":
            reco_msg = self._push_to_reco_engine(entry)
            message += "；" + reco_msg
        return entry, message

    def _push_to_query_cache(self, entry: dict[str, Any]) -> str:
        rows = store.rows("query_cache")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        qc_entry: dict[str, Any] = {
            "id": next_id,
            "status": "有效",
            "pending": True,
            "abnormal": False,
            "条目编号": f"QUER-PATH-{next_id:04d}",
            "查询模板": f"path_{entry.get('起始实体','')}_to_{entry.get('目标实体','')}",
            "结果哈希": f"reasoned_{entry.get('路径数量','')}",
            "命中次数": "0",
            "TTL秒数": "7200",
            "存储大小": "2KB",
            "最后命中": "",
            "缓存状态": "fresh",
        }
        rows.append(qc_entry)
        return f"推理结果已缓存 {qc_entry['条目编号']}"

    def _push_to_reco_engine(self, entry: dict[str, Any]) -> str:
        # 链路 L11：推理生成的整数候选数量，来源 inference；实际证据数量=路径数量。
        raw_paths = entry.get("路径数量", 10)
        paths = parse_count(raw_paths)
        reco_entry = ingest_runtime_rule(
            {
                "推荐场景": "路径推理关联推荐",
                "源实体": str(entry.get("起始实体", "")),
                "推荐算法": f"path-rank-{entry.get('推理模式','')}",
                "候选数量": (paths + 5) if paths is not None else raw_paths,
                "过滤条件": str(entry.get("置信阈值", "")),
                "命中次数": "0",
                "采纳率": "0%",
            },
            source="inference",
            evidence=paths if paths is not None else 0,
            code_prefix="RECO-PATH",
        )
        return f"路径推理结果推送推荐引擎 {reco_entry['规则编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["query_cache", "reco_engine"]
        return base