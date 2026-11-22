"""推荐引擎候选限额接口：v2 解码读取、存量版本迁移、旧版回滚读取。

注意：本路由必须先于通用资源路由注册，否则 /api/reco_engine/candidates
会被 /api/{module}/{entry_id} 抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult
from app.services import service_for
from app.services.reco_engine import LIMIT_VERSION, RecoEngineService, migrate_stock

router = APIRouter(prefix="/api/reco_engine", tags=["推荐引擎候选限额"])


def _reco_service() -> RecoEngineService:
    service = service_for("reco_engine")
    if not isinstance(service, RecoEngineService):
        raise HTTPException(status_code=404, detail="推荐引擎服务未注册")
    return service


@router.get("/candidates", response_model=dict)
def read_candidates(
    scene: str | None = Query(default=None),
    entity: str | None = Query(default=None),
    version: int = Query(default=LIMIT_VERSION, ge=1, le=LIMIT_VERSION),
    sync: bool = Query(default=True),
) -> dict[str, Any]:
    """候选读取接口：返回被纳入的候选数据和被搁置的记录。

    - version=2（默认）：按二版带来源的候选限额解码，并把解码结果同步到
      路径推理的待处理清单（sync=false 可关闭）；
    - version=1：回退到旧版读取语义，直接读原字面，不触发迁移，来源仍可追溯。
    """
    service = _reco_service()
    decode = service.decode_candidates(scene=scene, entity=entity, version=version)
    if version >= LIMIT_VERSION and sync:
        decode["path_reason_sync"] = service.sync_decode_to_path_reason(decode)
    return decode


@router.post("/migration", response_model=ActionResult)
def run_migration() -> ActionResult:
    """对存量推荐规则做 v1→v2 版本迁移；幂等，可重复执行。"""
    stats = migrate_stock()
    return ActionResult(
        ok=True,
        message=(
            f"候选限额 v{LIMIT_VERSION} 迁移完成：共 {stats['total']} 条，"
            f"新迁移 {stats['migrated']} 条，已是最新 {stats['skipped']} 条"
        ),
        entry=stats,
    )
