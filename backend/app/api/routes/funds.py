"""ETF、LOF 与开放式基金行情查询 API。"""

import logging
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.fund_schemas import (
    FundBarResponse, FundInstrumentResponse, FundIntradayResponse,
    FundNavResponse, FundQuoteResponse,
)
from app.fund_data.models import FundDataError
from app.fund_data.service import FundMarketDataServiceError

router = APIRouter()
logger = logging.getLogger(__name__)
FundTypeParam = Literal["etf", "lof", "open_end"]


def _service(request: Request):
    if not request.app.state.database_ready:
        raise HTTPException(status_code=503, detail="SQL Server 尚未就绪，暂时无法读取基金数据")
    return request.app.state.fund_market_service


def _call(action):
    """将数据标准化错误转换为 422，并屏蔽数据库和上游堆栈细节。"""

    try:
        return action()
    except FundDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FundMarketDataServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("基金行情 API 请求失败")
        raise HTTPException(status_code=503, detail="基金行情暂不可用，请检查数据源或 SQL Server") from exc


@router.get("/funds/search", response_model=list[FundInstrumentResponse])
def search_funds(
    request: Request,
    q: str = Query(default="", max_length=40),
    fund_type: FundTypeParam | None = None,
    limit: int = Query(default=100, ge=1, le=200),
):
    return _call(lambda: _service(request).search(q, fund_type, limit))


@router.get("/funds/{fund_type}/{symbol}/quote", response_model=FundQuoteResponse)
def get_fund_quote(fund_type: FundTypeParam, symbol: str, request: Request, force_refresh: bool = False):
    return _call(lambda: _service(request).get_quote(fund_type, symbol, force_refresh))


@router.get("/funds/{fund_type}/{symbol}/bars", response_model=list[FundBarResponse])
def get_fund_bars(
    fund_type: FundTypeParam, symbol: str, request: Request,
    start_date: date, end_date: date,
):
    return _call(lambda: _service(request).get_bars(fund_type, symbol, start_date, end_date))


@router.get("/funds/{fund_type}/{symbol}/intraday", response_model=list[FundIntradayResponse])
def get_fund_intraday(fund_type: FundTypeParam, symbol: str, request: Request):
    return _call(lambda: _service(request).get_intraday(fund_type, symbol))


@router.get("/funds/{fund_type}/{symbol}/nav", response_model=list[FundNavResponse])
def get_fund_nav(
    fund_type: FundTypeParam, symbol: str, request: Request,
    start_date: date | None = None, end_date: date | None = None,
):
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="净值历史开始日期不能晚于结束日期")
    return _call(lambda: _service(request).get_nav(fund_type, symbol, start_date, end_date))
