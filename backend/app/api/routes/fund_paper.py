"""基金纸面实验多任务 API。"""

import logging

from fastapi import APIRouter, HTTPException, Request

from app.api.fund_paper_schemas import FundPaperCreateRequest, FundPaperSessionResponse
from app.fund_data.models import FundDataError

router = APIRouter()
logger = logging.getLogger(__name__)


def _manager(request: Request):
    if not request.app.state.database_ready:
        raise HTTPException(status_code=503, detail="SQL Server 尚未就绪，暂时无法读取基金实验")
    return request.app.state.fund_paper_manager


@router.get("/fund-paper/strategies")
def get_fund_paper_strategies(request: Request):
    return _manager(request).get_strategies()


@router.get("/fund-paper/sessions", response_model=list[FundPaperSessionResponse])
def list_fund_paper_sessions(request: Request):
    try:
        return _manager(request).list_sessions()
    except Exception as exc:
        logger.exception("读取基金实验列表失败")
        raise HTTPException(status_code=503, detail="读取基金实验失败，请检查 SQL Server") from exc


@router.post("/fund-paper/sessions", response_model=FundPaperSessionResponse, status_code=201)
def create_fund_paper_session(payload: FundPaperCreateRequest, request: Request):
    manager = _manager(request)
    try:
        session_id = manager.create_experiment(payload.task_name, payload.fund_type, payload.symbols,
            payload.strategy_code, payload.initial_capital, payload.parameters)
        result = manager.get_session(session_id)
        if result is None:
            raise RuntimeError("新建实验后无法读取任务")
        return result
    except FundDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("创建基金实验失败")
        raise HTTPException(status_code=503, detail="创建基金实验失败，请检查数据源或 SQL Server") from exc


@router.get("/fund-paper/sessions/{session_id}", response_model=FundPaperSessionResponse)
def get_fund_paper_session(session_id: str, request: Request):
    try:
        result = _manager(request).get_session(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="基金实验不存在")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("读取基金实验详情失败")
        raise HTTPException(status_code=503, detail="读取基金实验失败，请检查 SQL Server") from exc


@router.post("/fund-paper/sessions/{session_id}/stop", response_model=FundPaperSessionResponse)
def stop_fund_paper_session(session_id: str, request: Request):
    try:
        manager = _manager(request)
        if not manager.stop_session(session_id):
            if manager.get_session(session_id) is None:
                raise HTTPException(status_code=404, detail="基金实验不存在")
            raise HTTPException(status_code=409, detail="该基金实验已停止或结束")
        return manager.get_session(session_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("停止基金实验失败")
        raise HTTPException(status_code=503, detail="停止基金实验失败，请检查 SQL Server") from exc


@router.delete("/fund-paper/sessions/{session_id}")
def delete_fund_paper_session(session_id: str, request: Request):
    """仅允许删除已停止任务；运行中任务由仓储的行锁校验后拒绝。"""

    manager = _manager(request)
    try:
        if not manager.delete_session(session_id):
            current = manager.get_session(session_id)
            if current is None:
                raise HTTPException(status_code=404, detail="基金实验不存在")
            raise HTTPException(status_code=409, detail="仅已停止的基金实验可以删除")
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("删除基金实验失败")
        raise HTTPException(status_code=503, detail="删除基金实验失败，请检查 SQL Server") from exc
