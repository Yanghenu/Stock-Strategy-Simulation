"""兼容旧月度会话，并提供多任务纸面实验 API。"""

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from app.api.schemas import PaperExperimentRequest, PaperSessionAccepted, PaperStartRequest

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_database(request: Request, action: str) -> None:
    """任务账本必须通过已初始化的 SQL Server 读写。"""

    if not request.app.state.database_ready:
        raise HTTPException(status_code=503, detail=f"SQL Server 尚未就绪，暂时无法{action}纸面实验")


@router.post(
    "/paper-trading/start",
    response_model=PaperSessionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_paper_trading(payload: PaperStartRequest, request: Request):
    """创建实验或返回已在运行的会话，避免重复点击生成两套账户。"""

    if not request.app.state.database_ready:
        raise HTTPException(status_code=503, detail="SQL Server 尚未就绪，暂时无法启动纸面实验")
    session_id = request.app.state.paper_trading_manager.create_session(payload.initial_capital)
    return {"session_id": session_id, "status": "running"}


@router.get("/paper-trading/current")
def get_current_paper_trading(request: Request):
    """返回当前或最近一次实验的现金、持仓、净值曲线和成交记录。"""

    if not request.app.state.database_ready:
        raise HTTPException(status_code=503, detail="SQL Server 尚未就绪，暂时无法读取纸面实验")
    return request.app.state.paper_trading_manager.get_current_session()


@router.get("/paper-trading/strategies")
def list_paper_strategies(request: Request):
    """返回可选策略、说明及当前默认周期。"""

    return request.app.state.paper_trading_manager.get_strategies()


@router.get("/paper-trading/sessions")
def list_paper_sessions(request: Request):
    """返回运行与历史任务的账户摘要。"""

    _require_database(request, "读取")
    try:
        return request.app.state.paper_trading_manager.list_sessions()
    except Exception as exc:
        logger.exception("读取纸面实验任务列表失败")
        raise HTTPException(status_code=503, detail="读取纸面实验失败，请检查 SQL Server 连接") from exc


@router.post(
    "/paper-trading/sessions",
    response_model=PaperSessionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_paper_session(payload: PaperExperimentRequest, request: Request):
    """创建独立账户；名称、股票池和策略参数由 SQL Server 持久化。"""

    _require_database(request, "创建")
    manager = request.app.state.paper_trading_manager
    strategy_codes = {item["code"] for item in manager.get_strategies()}
    if payload.strategy_code not in strategy_codes:
        raise HTTPException(status_code=422, detail="未知的纸面实验策略")
    try:
        session_id = manager.create_experiment(
            payload.task_name, payload.symbols, payload.strategy_code, payload.initial_capital,
            payload.parameters,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("创建纸面实验任务失败")
        raise HTTPException(status_code=503, detail="创建纸面实验失败，请检查 SQL Server 连接") from exc
    return {"session_id": session_id, "status": "running"}


@router.get("/paper-trading/sessions/{session_id}")
def get_paper_session(session_id: UUID, request: Request):
    """读取一个任务的策略参数、持仓、净值曲线和成交。"""

    _require_database(request, "读取")
    try:
        session = request.app.state.paper_trading_manager.get_session(str(session_id))
    except Exception as exc:
        logger.exception("读取纸面实验详情失败，session_id=%s", session_id)
        raise HTTPException(status_code=503, detail="读取纸面实验失败，请检查 SQL Server 连接") from exc
    if session is None:
        raise HTTPException(status_code=404, detail="纸面实验任务不存在")
    return session


@router.post("/paper-trading/sessions/{session_id}/stop")
def stop_paper_session(session_id: UUID, request: Request):
    """停止指定运行任务；已结束账本继续保留以供查看和比较。"""

    _require_database(request, "停止")
    manager = request.app.state.paper_trading_manager
    normalized_id = str(session_id)
    try:
        session = manager.get_session(normalized_id)
        if session is None:
            raise HTTPException(status_code=404, detail="纸面实验任务不存在")
        if session["status"] != "running":
            raise HTTPException(status_code=409, detail="纸面实验任务已经结束")
        if not manager.stop_session(normalized_id):
            raise HTTPException(status_code=409, detail="纸面实验任务状态已变化，请刷新后重试")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("停止纸面实验失败，session_id=%s", normalized_id)
        raise HTTPException(status_code=503, detail="停止纸面实验失败，请检查 SQL Server 连接") from exc
    return {"session_id": normalized_id, "status": "stopped"}
