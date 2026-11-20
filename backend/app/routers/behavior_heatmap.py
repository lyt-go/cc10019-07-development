"""行为热力路由：生成热力图并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.behavior_heatmap import generate_heatmap, get_heatmap
from app.store import store

router = APIRouter(prefix="/api/behavior-heatmap", tags=["behavior_heatmap"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("behavior_heatmap")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}




@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("behavior_heatmap")
    return {"items": rows, "total": len(rows)}


@router.post("/{config_id}/generate")
def generate(
    config_id: str,
    start: str | None = None,
    end: str | None = None,
    version: str | None = None,
) -> ActionResult:
    try:
        result = generate_heatmap(config_id, start=start, end=end, version=version)
        return ActionResult(ok=True, message="Generated", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{config_id}/result")
def result(config_id: str) -> dict[str, Any]:
    res = get_heatmap(config_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("behavior_heatmap", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
