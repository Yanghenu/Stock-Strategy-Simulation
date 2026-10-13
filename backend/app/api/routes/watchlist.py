"""行情关注列表 API；所有关注项都持久化在 SQL Server。"""

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.schemas import WatchlistAddRequest, WatchlistItemResponse
from app.market_data.service import MarketDataServiceError, normalize_symbol

router = APIRouter()


def _require_database(request: Request) -> None:
    """数据库未就绪时明确返回 503，避免把关注写入临时缓存。"""

    if not request.app.state.database_ready:
        raise HTTPException(status_code=503, detail="SQL Server 尚未就绪，暂时无法读取关注列表")


def _normalized_symbol(value: str) -> str:
    try:
        return normalize_symbol(value)
    except MarketDataServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/watchlist", response_model=list[WatchlistItemResponse])
def get_watchlist(request: Request):
    """读取持久化关注列表，按加入时间展示。"""

    _require_database(request)
    return request.app.state.repository.list_watchlist()


@router.post("/watchlist", response_model=WatchlistItemResponse)
def add_watchlist_item(payload: WatchlistAddRequest, request: Request):
    """添加或更新关注项，重复点击保持幂等。"""

    _require_database(request)
    symbol = _normalized_symbol(payload.symbol)
    request.app.state.repository.add_watchlist(symbol, payload.name)
    return {"symbol": symbol, "name": payload.name}


@router.delete("/watchlist/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
def remove_watchlist_item(symbol: str, request: Request) -> Response:
    """取消关注；目标已经不存在时仍返回成功，支持安全重试。"""

    _require_database(request)
    request.app.state.repository.remove_watchlist(_normalized_symbol(symbol))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
