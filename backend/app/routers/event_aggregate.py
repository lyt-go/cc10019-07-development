"""事件聚合路由：执行事件聚合并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.event_aggregate import (
    aggregate_events,
    backfill_hour,
    get_aggregate_result,
    query_aggregate_detail,
)
from app.store import store

router = APIRouter(prefix="/api/event-aggregate", tags=["event_aggregate"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("event_aggregate")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}




@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("event_aggregate")
    return {"items": rows, "total": len(rows)}


@router.get("/{config_id}/detail")
def detail_range(
    config_id: str,
    start: str,
    end: str,
    version: str | None = None,
    dimension: str | None = None,
    dimension_value: str | None = None,
) -> dict[str, Any]:
    """按开始/结束时间查询事件聚合明细，按小时桶返回覆盖情况。"""
    try:
        return query_aggregate_detail(
            config_id,
            start=start,
            end=end,
            version=version,
            dimension=dimension,
            dimension_value=dimension_value,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{config_id}/backfill")
def backfill(config_id: str, hour: int) -> ActionResult:
    """补齐某个小时的迟到事件，并发布新的聚合版本。"""
    try:
        result = backfill_hour(config_id, hour)
        return ActionResult(ok=True, message="Backfilled", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("event_aggregate", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{config_id}/run")
def run(config_id: str) -> ActionResult:
    try:
        result = aggregate_events(config_id)
        return ActionResult(ok=True, message="Aggregated", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{config_id}/result")
def result(config_id: str) -> dict[str, Any]:
    res = get_aggregate_result(config_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("event_aggregate", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
