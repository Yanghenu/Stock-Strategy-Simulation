"""股票搜索、实时快照和历史日线 API。"""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Query, Request

from app.api.schemas import BarResponse, IntradayBarResponse, QuoteResponse
from app.market_data.service import normalize_symbol

router = APIRouter()


@router.get("/stocks/search", response_model=list[QuoteResponse])
def search_stocks(request: Request, q: str = Query(default="", max_length=40)):
    return request.app.state.market_service.search_stocks(q)


@router.get("/market/quotes/{symbol}", response_model=QuoteResponse)
def get_quote(symbol: str, request: Request, force_refresh: bool = False):
    # 路由层先验证路径参数，保证真实服务与替身服务返回一致的客户端错误。
    return request.app.state.market_service.get_quote(normalize_symbol(symbol), force_refresh=force_refresh)


@router.get("/stocks/{symbol}/bars", response_model=list[BarResponse])
def get_bars(
    symbol: str,
    request: Request,
    start_date: date,
    end_date: date,
    adjust: Literal["raw", "qfq"] = "qfq",
):
    normalized_symbol = normalize_symbol(symbol)
    bars = request.app.state.market_service.get_bars(normalized_symbol, start_date, end_date)
    return [
        BarResponse(
            trade_date=item.trade_date,
            open=item.adj_open if adjust == "qfq" else item.open,
            high=item.adj_high if adjust == "qfq" else item.high,
            low=item.adj_low if adjust == "qfq" else item.low,
            close=item.adj_close if adjust == "qfq" else item.close,
            volume=item.volume,
            turnover=item.turnover,
        )
        for item in bars
    ]


@router.get("/stocks/{symbol}/intraday", response_model=list[IntradayBarResponse])
def get_intraday_bars(symbol: str, request: Request):
    """返回上海时区当天的分钟数据，盘前或休市可返回空数组。"""

    return request.app.state.market_service.get_intraday_bars(normalize_symbol(symbol))
