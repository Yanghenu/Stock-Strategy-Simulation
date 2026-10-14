"""FastAPI 应用装配与本机运行入口。"""

import logging
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import backtests, config, fund_paper as fund_paper_routes, funds, market, paper_trading, stock_screen, watchlist
from app.cache.redis_cache import CacheUnavailableError, RedisCache
from app.config import Settings, load_settings
from app.database import DatabaseUnavailableError
from app.market_data.akshare_source import AKShareSource, MarketDataSourceError
from app.market_data.service import MarketDataService, MarketDataServiceError
from app.fund_data.akshare_source import AKShareFundSource, FundMarketSourceError
from app.fund_data.models import FundDataError
from app.fund_data.service import FundMarketDataService, FundMarketDataServiceError
from app.repositories.sql_repository import SqlRepository
from app.repositories.fund_repository import FundRepository
from app.services.backtest_manager import BacktestManager
from app.paper_trading.manager import PaperTradingManager
from app.fund_paper.manager import FundPaperTradingManager
from app.stock_screen.manager import StockScreenManager
from app.stock_screen.repository import StockScreenRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    repository: Any | None = None,
    market_service: Any | None = None,
    backtest_manager: Any | None = None,
    paper_manager: Any | None = None,
    fund_repository: Any | None = None,
    fund_market_service: Any | None = None,
    fund_paper_manager: Any | None = None,
    stock_screen_manager: Any | None = None,
) -> FastAPI:
    """支持替换外部依赖，便于离线测试；默认组件在请求/启动边界延迟连接服务。"""

    resolved = settings or load_settings()
    repo = repository or SqlRepository(resolved.database)
    source = AKShareSource(resolved.market_data)
    cache = RedisCache(resolved.redis)
    market_data = market_service or MarketDataService(repo, source, cache, resolved)
    manager = backtest_manager or BacktestManager(repo, market_data)
    paper = paper_manager or PaperTradingManager(repo, market_data, resolved)
    fund_repo = fund_repository or FundRepository(resolved.database)
    fund_source = AKShareFundSource(resolved.market_data)
    fund_market = fund_market_service or FundMarketDataService(fund_repo, fund_source, resolved.fund)
    fund_paper = fund_paper_manager or FundPaperTradingManager(fund_repo, fund_market, resolved)
    screen = stock_screen_manager or StockScreenManager(StockScreenRepository(repo), market_data, resolved.stock_screen)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.database_ready = False
        try:
            repo.initialize()
            manager.recover_interrupted_runs()
            application.state.database_ready = True
        except Exception:
            # 数据库短时不可用时仍允许实时行情页启动，依赖数据库的接口会返回 503。
            logger.exception("SQL Server 初始化不可用；数据库相关接口将暂不可用")
        if application.state.database_ready:
            try:
                paper.start()
            except Exception:
                logger.exception("股票纸面实验 worker 未能启动")
            try:
                fund_paper.start()
            except Exception:
                # 基金实验 worker 与既有股票实验分开启动，单独失败不停止股票实验。
                logger.exception("基金实验 worker 未能启动；基金任务暂不自动运行")
            if isinstance(repo, SqlRepository) or stock_screen_manager is not None:
                try:
                    screen.start()
                except Exception:
                    logger.exception("选股实验 worker 未能启动")
        try:
            yield
        finally:
            screen.shutdown()
            try:
                fund_paper.shutdown()
            except Exception:
                logger.exception("关闭基金实验 worker 失败")
            paper.shutdown()
            manager.shutdown()

    application = FastAPI(title="A 股量化回测工作台", version="1.0.0", lifespan=lifespan)
    application.state.settings = resolved
    application.state.repository = repo
    application.state.market_service = market_data
    application.state.backtest_manager = manager
    application.state.paper_trading_manager = paper
    application.state.fund_repository = fund_repo
    application.state.fund_market_service = fund_market
    application.state.fund_paper_manager = fund_paper
    application.state.stock_screen_manager = screen

    @application.exception_handler(DatabaseUnavailableError)
    async def database_error(_: Request, exc: DatabaseUnavailableError):
        return JSONResponse(status_code=503, content={"code": "DATABASE_UNAVAILABLE", "detail": str(exc)})

    @application.exception_handler(CacheUnavailableError)
    async def cache_error(_: Request, exc: CacheUnavailableError):
        return JSONResponse(status_code=503, content={"code": "CACHE_UNAVAILABLE", "detail": str(exc)})

    @application.exception_handler(MarketDataSourceError)
    async def market_source_error(_: Request, exc: MarketDataSourceError):
        return JSONResponse(status_code=503, content={"code": "MARKET_SOURCE_UNAVAILABLE", "detail": str(exc)})

    @application.exception_handler(MarketDataServiceError)
    async def market_service_error(_: Request, exc: MarketDataServiceError):
        message = str(exc)
        status_code = 422 if "必须是六位" in message or "日期不能晚" in message else 503
        return JSONResponse(status_code=status_code, content={"code": "MARKET_DATA_ERROR", "detail": message})

    @application.exception_handler(FundMarketSourceError)
    async def fund_source_error(_: Request, exc: FundMarketSourceError):
        return JSONResponse(status_code=503, content={"code": "FUND_SOURCE_UNAVAILABLE", "detail": str(exc)})

    @application.exception_handler(FundMarketDataServiceError)
    async def fund_service_error(_: Request, exc: FundMarketDataServiceError):
        return JSONResponse(status_code=503, content={"code": "FUND_DATA_UNAVAILABLE", "detail": str(exc)})

    @application.exception_handler(FundDataError)
    async def fund_data_error(_: Request, exc: FundDataError):
        return JSONResponse(status_code=422, content={"code": "FUND_DATA_INVALID", "detail": str(exc)})

    @application.get("/api/health")
    def health():
        return {
            "status": "ok" if application.state.database_ready else "degraded",
            "database": "ready" if application.state.database_ready else "unavailable",
        }

    application.include_router(config.router, prefix="/api")
    application.include_router(market.router, prefix="/api")
    application.include_router(backtests.router, prefix="/api")
    application.include_router(paper_trading.router, prefix="/api")
    application.include_router(watchlist.router, prefix="/api")
    application.include_router(funds.router, prefix="/api")
    application.include_router(fund_paper_routes.router, prefix="/api")
    application.include_router(stock_screen.router, prefix="/api")
    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host=app.state.settings.api_host, port=app.state.settings.api_port)
