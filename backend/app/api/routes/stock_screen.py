"""07 选股实验：创建、查看、取消研究扫描。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter()


class CreateScreenRequest(BaseModel):
    """只开放工作量参数；研究规则保持固定，避免看到结果后反复调参。"""

    universe_size: int | None = Field(default=None, ge=1, le=3000)


@router.post("/stock-screens", status_code=status.HTTP_202_ACCEPTED)
def create_screen(payload: CreateScreenRequest, request: Request):
    try:
        run_id = request.app.state.stock_screen_manager.create(payload.universe_size)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"run_id": run_id, "status": "queued"}


@router.get("/stock-screens")
def list_screens(request: Request):
    return request.app.state.stock_screen_manager.list_runs()


@router.get("/stock-screens/{run_id}")
def get_screen(run_id: UUID, request: Request):
    record = request.app.state.stock_screen_manager.get_run(str(run_id))
    if record is None:
        raise HTTPException(status_code=404, detail="找不到该选股实验")
    return record


@router.post("/stock-screens/{run_id}/cancel")
def cancel_screen(run_id: UUID, request: Request):
    manager = request.app.state.stock_screen_manager
    if manager.cancel(str(run_id)):
        return {"run_id": str(run_id), "status": "cancelled"}
    record = manager.get_run(str(run_id))
    if record is None:
        raise HTTPException(status_code=404, detail="找不到该选股实验")
    return {"run_id": str(run_id), "status": record["status"]}
