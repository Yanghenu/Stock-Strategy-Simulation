"""回测提交、进度、历史和取消 API。"""

from math import isfinite

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.api.schemas import BacktestAccepted, BacktestRequest
from app.backtest.engine import validate_backtest_params
from app.market_data.service import normalize_symbol
from app.models import BacktestParams

router = APIRouter()


def _params(payload: BacktestRequest, request: Request) -> BacktestParams:
    defaults = request.app.state.settings.backtest
    # 读取同一份纸面策略目录及其配置默认值，不修改任何运行中的任务状态。
    definitions = {item["code"]: item for item in request.app.state.paper_trading_manager.get_strategies()}
    definition = definitions.get(payload.strategy_code)
    if definition is None:
        raise HTTPException(status_code=422, detail="不支持的回测策略")
    # 仅允许当前策略目录列出的参数；复用纸面实验的范围与默认值，避免两处配置漂移。
    parameter_definitions = {item["key"]: item for item in definition["parameters"]}
    if set(payload.parameters) - set(parameter_definitions):
        raise HTTPException(status_code=422, detail="策略包含未知参数")
    resolved: dict[str, int | float] = {}
    for key, item in parameter_definitions.items():
        fallback = item["default"]
        if payload.strategy_code == "dual_ma" and key not in payload.parameters:
            fallback = (payload.short_window if key == "short_ma_period" else payload.long_window) or (
                defaults.short_window if key == "short_ma_period" else defaults.long_window
            )
        value = payload.parameters.get(key, fallback)
        # 回测旧接口曾允许 250/500 日均线，保持已有档案可重新提交。
        maximum = item["max"]
        if payload.strategy_code == "dual_ma":
            maximum = {"short_ma_period": 250, "long_ma_period": 500}.get(key, maximum)
        if not isfinite(value) or value < item["min"] or value > maximum:
            raise HTTPException(status_code=422, detail=f"{item['label']}超出允许范围")
        if item["type"] == "integer" and int(value) != value:
            raise HTTPException(status_code=422, detail=f"{item['label']}必须是整数")
        resolved[key] = int(value) if item["type"] == "integer" else float(value)

    symbol = normalize_symbol(payload.symbol)
    symbols = tuple(normalize_symbol(item) for item in (payload.symbols or [symbol]))
    if len(set(symbols)) != len(symbols) or symbols[0] != symbol:
        raise HTTPException(status_code=422, detail="股票列表不能重复，且第一只应与主股票代码一致")
    if payload.strategy_code != "momentum_rotation" and len(symbols) != 1:
        raise HTTPException(status_code=422, detail="该策略每次只能回测一只股票")
    params = BacktestParams(
        symbol=symbol,
        start_date=payload.start_date,
        end_date=payload.end_date,
        short_window=int(resolved.get("short_ma_period", defaults.short_window)),
        long_window=int(resolved.get("long_ma_period", defaults.long_window)),
        initial_capital=payload.initial_capital if payload.initial_capital is not None else defaults.initial_capital,
        position_ratio=payload.position_ratio,
        commission_rate=payload.commission_rate if payload.commission_rate is not None else defaults.commission_rate,
        minimum_commission=payload.minimum_commission if payload.minimum_commission is not None else defaults.minimum_commission,
        stamp_duty_rate=payload.stamp_duty_rate if payload.stamp_duty_rate is not None else defaults.stamp_duty_rate,
        slippage_rate=payload.slippage_rate if payload.slippage_rate is not None else defaults.slippage_rate,
        strategy_code=payload.strategy_code,
        symbols=symbols,
        ma_period=int(resolved.get("ma_period", 60)),
        breakout_period=int(resolved.get("breakout_period", 20)),
        atr_period=int(resolved.get("atr_period", 20)),
        atr_multiplier=float(resolved.get("atr_multiplier", 2.0)),
        momentum_period_days=int(resolved.get("momentum_period_days", 20)),
        trend_ma_period=int(resolved.get("trend_ma_period", 60)),
        max_positions=int(resolved.get("max_positions", 3)),
        rebalance_interval_days=int(resolved.get("rebalance_interval_days", 7)),
    )
    try:
        validate_backtest_params(params)
    except ValueError as exc:
        # 跨字段业务校验属于客户端输入错误，统一返回 422，避免误报成服务器故障。
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return params


@router.get("/backtests/strategies")
def list_backtest_strategies(request: Request):
    """回测与纸面实验共享策略目录，但回测执行仍使用隔日开盘撮合。"""

    return request.app.state.paper_trading_manager.get_strategies()


@router.post("/backtests", response_model=BacktestAccepted, status_code=status.HTTP_202_ACCEPTED)
def submit_backtest(payload: BacktestRequest, request: Request):
    params = _params(payload, request)
    run_id = request.app.state.backtest_manager.submit(params)
    return {"run_id": run_id, "status": "queued"}


@router.get("/backtests")
def list_backtests(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    items, total = request.app.state.repository.list_backtests(offset, limit)
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@router.get("/backtests/{run_id}")
def get_backtest(run_id: str, request: Request):
    record = request.app.state.repository.get_backtest(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="找不到该回测记录")
    return record


@router.post("/backtests/{run_id}/cancel")
def cancel_backtest(run_id: str, request: Request):
    if request.app.state.backtest_manager.cancel(run_id):
        return {"run_id": run_id, "requested": True}
    record = request.app.state.repository.get_backtest(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="找不到该回测记录")
    return {"run_id": run_id, "requested": False, "status": record["status"]}
