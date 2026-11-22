"""推荐引擎领域服务：候选数量 v1→v2 版本迁移、带来源的候选限额、候选解码与同步。

背景：旧版（v1）推荐规则的「候选数量」在示例数据里是占位字符串；运行时写入的
规则既有路径推理生成的整数，也有子图探查带入的数量。本模块把存量迁移为二版
（v2）带来源的候选限额（「候选限额」字段），并提供候选读取能力：

- 旧值能转换时沿用真实数量含义；无法转换的未知值不落零、也不放全量，按搁置处理；
- 原字面和来源保留在限额记录里，回滚读取（version=1）或重复迁移都可追溯；
- 同一源实体的新旧两种结果，在同一次候选计算里只计入一条，不重复计入；
- 解码结果同步到路径推理的待处理清单，只有实际证据数量大于 0 的规则能继续使用；
- 旧值回填与新记录写入共用一把锁，并发执行安全且幂等。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

MODULE_KEY = "reco_engine"
LIMIT_FIELD = "候选限额"
LIMIT_VERSION = 2

# 来源优先级：推理输入（新版上线期间写入）> 探查带入 > 手工登记 > 存量旧值。
SOURCE_PRIORITY = {"inference": 30, "probe": 20, "manual": 10, "legacy": 0}

# 旧值回填与新记录写入并发时的公共锁：保证迁移幂等、行 id 分配唯一。
_LIMIT_LOCK = threading.RLock()

_INT_LITERAL = re.compile(r"^[+-]?\d+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_count(raw: Any) -> int | None:
    """把旧版候选数量字面量解析为整数；无法解析返回 None（未知）。

    未知不等于 0，也不等于完整候选集——调用方必须把 None 当搁置信号处理。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        text = raw.strip()
        if _INT_LITERAL.match(text):
            return int(text)
    return None


def infer_source(rule_no: Any) -> str:
    """按规则编号前缀推断存量记录来源，供迁移回填使用。"""
    text = str(rule_no or "")
    if text.startswith("RECO-PATH"):
        return "inference"
    if text.startswith("RECO-SUB"):
        return "probe"
    return "legacy"


def build_limit(raw: Any, source: str, evidence: int | None = None) -> dict[str, Any]:
    """构造 v2 候选限额记录：值、来源、实际证据数量、原字面一并留档。"""
    value = parse_count(raw)
    if evidence is None:
        evidence = value if (value is not None and value > 0) else 0
    return {
        "version": LIMIT_VERSION,
        "value": value,  # None 表示未知：不落零、不视为全量候选
        "source": source,
        "evidence": max(int(evidence), 0),
        "raw": raw,  # 迁移前原字面，回滚读取/重复迁移可追溯
        "recorded_at": _now(),
    }


def ensure_limit_v2(row: dict[str, Any]) -> bool:
    """把单条规则回填成 v2 候选限额；已是 v2 则跳过（幂等）。返回是否新迁移。"""
    existing = row.get(LIMIT_FIELD)
    if isinstance(existing, dict) and existing.get("version") == LIMIT_VERSION:
        return False
    row[LIMIT_FIELD] = build_limit(row.get("候选数量"), infer_source(row.get("规则编号")))
    return True


def migrate_stock() -> dict[str, int]:
    """对存量推荐规则做 v1→v2 版本迁移；可重复执行，重复执行结果不变。"""
    with _LIMIT_LOCK:
        rows = store.rows(MODULE_KEY)
        migrated = sum(1 for row in rows if ensure_limit_v2(row))
        return {"total": len(rows), "migrated": migrated, "skipped": len(rows) - migrated}


def ingest_runtime_rule(
    fields: dict[str, Any], *, source: str, evidence: int, code_prefix: str
) -> dict[str, Any]:
    """运行时新记录写入（推理/探查推送）：直接落 v2 限额，与旧值回填并发安全。"""
    with _LIMIT_LOCK:
        rows = store.rows(MODULE_KEY)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry: dict[str, Any] = {"id": next_id, "status": "草稿", "pending": True, "abnormal": False}
        entry.update(fields)
        entry["规则编号"] = f"{code_prefix}-{next_id:04d}"
        entry[LIMIT_FIELD] = build_limit(entry.get("候选数量"), source, evidence)
        rows.append(entry)
        return entry


def _rank(view: dict[str, Any]) -> tuple[bool, bool, int, int]:
    """同源实体组内挑选有效记录的优先级：有证据 > 仅已知 > 来源新 > 记录新。"""
    known = view["value"] is not None
    evidenced = known and view["evidence"] > 0
    return (evidenced, known, SOURCE_PRIORITY.get(view["source"], 0), int(view["id"]))


class RecoEngineService(BaseModuleService):
    MODULE = MODULE_KEY
    ENTITY = "推荐规则"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["规则编号", "推荐场景", "源实体", "推荐算法", "候选数量", "过滤条件", "命中次数", "采纳率"]
    REQUIRED_FIELDS = ["规则编号", "推荐场景", "源实体"]
    STATUS_ORDER = ["草稿", "已生效", "效果差", "已停用"]
    ACTION_RULES = {"启效规则": "已生效", "标记差效": "效果差", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # ===== v2 候选限额：登记、迁移、解码、同步 =====

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 与运行时推送共用同一把锁，保证并发登记时 id 唯一、限额同步落档。
        with _LIMIT_LOCK:
            entry, missing = super().create_entry(values)
            if entry is not None:
                entry[LIMIT_FIELD] = build_limit(entry.get("候选数量"), "manual")
            return entry, missing

    def migrate(self) -> dict[str, int]:
        return migrate_stock()

    def _limit_view(self, row: dict[str, Any], version: int) -> dict[str, Any]:
        """生成单条规则的限额视图：v2 读候选限额，v1 回滚读原字面。

        回滚读取（version=1）时来源仍从已迁移的 v2 记录（或编号推断）取得，
        原字面就是「候选数量」字段本身——迁移从不改写它。
        """
        limit = row.get(LIMIT_FIELD)
        has_v2 = isinstance(limit, dict) and limit.get("version") == LIMIT_VERSION
        if version >= LIMIT_VERSION and has_v2:
            value, evidence = limit.get("value"), int(limit.get("evidence", 0))
            source, raw = limit.get("source"), limit.get("raw")
        else:
            raw = row.get("候选数量")
            value = parse_count(raw)
            evidence = value if (value is not None and value > 0) else 0
            source = limit.get("source") if has_v2 else infer_source(row.get("规则编号"))
        return {
            "id": int(row.get("id", 0)),
            "规则编号": row.get("规则编号"),
            "推荐场景": row.get("推荐场景"),
            "源实体": row.get("源实体"),
            "status": row.get("status"),
            "value": value,
            "evidence": evidence,
            "source": source,
            "raw": raw,
        }

    def decode_candidates(
        self,
        *,
        scene: str | None = None,
        entity: str | None = None,
        version: int = LIMIT_VERSION,
    ) -> dict[str, Any]:
        """候选解码：把推荐清单算成「纳入的候选数据 + 搁置的记录」。

        - version=2：先幂等回填存量，再按 v2 限额解码；
        - version=1：回滚到旧版语义，直接读原字面，不触发迁移；
        - 同一源实体只计入一条有效记录，新旧两种结果不重复计入；
        - 未知限额（None）搁置，不落零、不放全量；证据数量为 0 的规则同样搁置。
        """
        if version >= LIMIT_VERSION:
            migrate_stock()
        rows = list(store.rows(self.MODULE))
        if scene:
            rows = [r for r in rows if scene.casefold() in str(r.get("推荐场景", "")).casefold()]
        if entity:
            rows = [r for r in rows if entity.casefold() in str(r.get("源实体", "")).casefold()]

        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            view = self._limit_view(row, version)
            groups.setdefault(str(view["源实体"]), []).append(view)

        included: list[dict[str, Any]] = []
        shelved: list[dict[str, Any]] = []
        for members in groups.values():
            effective = max(members, key=_rank)
            for view in members:
                if view is not effective:
                    shelved.append({
                        **view,
                        "dedup_of": effective["规则编号"],
                        "reason": (
                            f"同一源实体已由 {effective['规则编号']}"
                            f"（来源 {effective['source']}）计入，本记录去重搁置"
                        ),
                    })
            if effective["value"] is None:
                shelved.append({
                    **effective,
                    "reason": (
                        f"候选限额未知：原字面「{effective['raw']}」无法转换，"
                        "按未知搁置（不落零、不放全量）"
                    ),
                })
            elif effective["evidence"] <= 0:
                shelved.append({
                    **effective,
                    "reason": "实际证据数量为 0，规则暂不可继续使用",
                })
            else:
                included.append(effective)

        included.sort(key=lambda v: v["id"])
        shelved.sort(key=lambda v: v["id"])
        return {
            "version": version,
            "scene": scene,
            "entity": entity,
            "included": [
                {**v, "limit": v["value"]} for v in included
            ],
            "shelved": shelved,
            "total_limit": sum(int(v["value"]) for v in included),
            "candidate_rule_count": len(included),
            "policy": {
                "unknown": "shelved",  # 未知值不落零、不放完整候选集
                "dedupe": "同一源实体单条计入",
                "evidence": "实际证据数量>0 的规则才能继续使用",
            },
        }

    def sync_decode_to_path_reason(self, decode: dict[str, Any]) -> dict[str, int]:
        """把候选解码结果同步到路径推理的待处理清单（幂等）。

        只同步被纳入的规则（限额已知且实际证据数量 > 0）；路径数量记录实际
        证据数量，供路径推理继续处理。已存在的解码任务不重复写入。
        """
        synced = skipped = 0
        with _LIMIT_LOCK:
            rows = store.rows("path_reason")
            existing = {str(r.get("任务编号")) for r in rows}
            for item in decode.get("included", []):
                task_no = f"PATH-DECO-{item['规则编号']}"
                if task_no in existing:
                    skipped += 1
                    continue
                next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
                rows.append({
                    "id": next_id,
                    "status": "待推理",
                    "pending": True,
                    "abnormal": False,
                    "任务编号": task_no,
                    "起始实体": item["源实体"],
                    "目标实体": f"推荐规则:{item['规则编号']}",
                    "推理模式": "candidate-decode",
                    "最大跳数": "2",
                    "路径数量": item["evidence"],
                    "置信阈值": "",
                    "推理耗时": "0",
                })
                existing.add(task_no)
                synced += 1
        return {"synced": synced, "skipped": skipped}

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        v2 = [
            row[LIMIT_FIELD] for row in rows
            if isinstance(row.get(LIMIT_FIELD), dict)
            and row[LIMIT_FIELD].get("version") == LIMIT_VERSION
        ]
        base["candidate_limit"] = {
            "version": LIMIT_VERSION,
            "migrated": len(v2),
            "legacy_pending": len(rows) - len(v2),
            "unknown": sum(1 for lim in v2 if lim.get("value") is None),
        }
        return base
