"""基金实验独立 worker；账户、订单和成交只写 fund_paper_* 表。"""

import hashlib
import logging
import math
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from app.config import Settings
from app.fund_data.models import FundBar, FundDataError, FundNavPoint
from app.fund_data.service import normalize_fund_symbol
from app.paper_trading.strategy import get_strategy_catalog, rank_momentum_rotation, should_exit
from app.fund_paper.strategy import (
    FUND_PORTFOLIO_STRATEGY_CODES, execute_exchange_trade, execute_open_nav_trade,
    get_fund_strategy_catalog, portfolio_target_weights, strategy_signal,
)

logger = logging.getLogger(__name__)
_CHINA_TZ = timezone(timedelta(hours=8))
_LEGACY_STRATEGIES = {str(item["code"]): item for item in get_strategy_catalog()}
_FUND_STRATEGIES = {str(item["code"]): item for item in get_fund_strategy_catalog()}
# 旧代码继续供已运行/已保存任务及兼容 API 使用，新建页面优先展示基金专用策略。
_STRATEGIES = {**_LEGACY_STRATEGIES, **_FUND_STRATEGIES}
_PARAM_COLUMNS = {
    "ma_period", "breakout_period", "atr_period", "atr_multiplier", "short_ma_period", "long_ma_period",
    "momentum_period_days", "trend_ma_period", "max_positions", "rebalance_interval_days", "risk_lookback_days",
}


def _china_time(value: datetime) -> datetime:
    return value.astimezone(_CHINA_TZ) if value.tzinfo else value.replace(tzinfo=_CHINA_TZ)


def _plain(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {name: getattr(value, name) for name in (
        "symbol", "name", "fund_type", "category", "subscription_status", "redemption_status", "fee_description",
    ) if hasattr(value, name)}


def _is_exchange_open(now: datetime) -> bool:
    """用本地交易时段限制场内订单，worker 不在闭市时补做模拟成交。"""

    local = _china_time(now)
    if local.weekday() >= 5:
        return False
    minute = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= minute < 11 * 60 + 30 or 13 * 60 <= minute < 15 * 60


def _normalize_strategy_parameters(strategy_code: str, supplied: dict[str, Any], symbol_count: int) -> dict[str, int | float]:
    strategy = _STRATEGIES.get(strategy_code)
    if strategy is None:
        raise FundDataError("所选策略不存在")
    definitions = {str(item["key"]): item for item in strategy["parameters"]}
    if any(key not in definitions for key in supplied):
        raise FundDataError("策略参数包含未知字段")
    values: dict[str, int | float] = {}
    for key, definition in definitions.items():
        raw = supplied.get(key, definition["default"])
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise FundDataError(f"策略参数 {key} 必须是数字") from exc
        if not math.isfinite(value) or value < float(definition["min"]) or value > float(definition["max"]):
            raise FundDataError(f"策略参数 {key} 超出允许范围")
        if definition["type"] == "integer":
            if not value.is_integer():
                raise FundDataError(f"策略参数 {key} 必须是整数")
            values[key] = int(value)
        else:
            values[key] = value
    if strategy_code == "dual_ma" and values["short_ma_period"] >= values["long_ma_period"]:
        raise FundDataError("短均线周期必须小于长均线周期")
    if strategy_code in {"momentum_rotation", "fund_relative_strength"} and values["max_positions"] > symbol_count:
        values["max_positions"] = symbol_count
    return values


class FundPaperTradingManager:
    """多任务基金 worker；每轮只处理运行中的基金任务并可独立停止。"""

    def __init__(self, repository: Any, market_service: Any, settings: Settings, clock: Callable[[], datetime] | None = None) -> None:
        self._repository = repository
        self._market = market_service
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(_CHINA_TZ))
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def get_strategies() -> list[dict[str, Any]]:
        catalog = get_fund_strategy_catalog()
        # 旧任务仍需显示原策略名称和参数，但前端会将这些选项标为不可新建。
        legacy = get_strategy_catalog()
        for item in legacy:
            item["available_for_new_tasks"] = False
            item["description"] = str(item["description"]).replace("股票", "基金") + "（仅供已有任务兼容）"
        return catalog + legacy

    def create_experiment(self, task_name: str, fund_type: str, symbols: list[str], strategy_code: str,
                          initial_capital: float, parameters: dict[str, Any] | None = None) -> str:
        """校验基金类型、申购状态与策略参数后，在独立 SQL 账本创建任务。"""

        if fund_type not in {"etf", "lof", "open_end"}:
            raise FundDataError("实验仅支持 ETF、LOF 或开放式基金")
        if not isinstance(task_name, str) or not task_name.strip() or len(task_name.strip()) > 100:
            raise FundDataError("实验名称不能为空且不能超过 100 个字符")
        if isinstance(initial_capital, bool) or not math.isfinite(float(initial_capital)) or initial_capital <= 0:
            raise FundDataError("初始资金必须大于 0")
        normalized = [normalize_fund_symbol(item) for item in symbols]
        if not 1 <= len(normalized) <= 20 or len(set(normalized)) != len(normalized):
            raise FundDataError("每个实验需选择 1 至 20 支不重复的基金")
        param_values = _normalize_strategy_parameters(strategy_code, parameters or {}, len(normalized))
        instruments: list[dict[str, Any]] = []
        for symbol in normalized:
            match = next((item for item in self._market.search(symbol, fund_type, 20)
                          if _plain(item).get("symbol") == symbol), None)
            if match is None:
                raise FundDataError(f"基金 {symbol} 在所选类型中不存在或暂不可查询")
            instrument = _plain(match)
            if fund_type == "open_end":
                category = str(instrument.get("category", "")).strip()
                if not category or any(word in category for word in ("货币", "封闭", "定开")):
                    raise FundDataError(f"{symbol} 不是支持申赎的普通开放式基金")
                # 创建任务只需确认可以申购；赎回暂停时仍可建任务，卖出信号会在交易环节等待开放。
                if "开放申购" not in str(instrument.get("subscription_status", "")):
                    raise FundDataError(f"{symbol} 当前不开放申购")
            instruments.append(instrument)
        now = _china_time(self._clock())
        payload: dict[str, Any] = {
            "task_name": task_name.strip(), "fund_type": fund_type, "strategy_code": strategy_code,
            "started_at": now, "initial_capital": float(initial_capital), "cash": float(initial_capital),
            "market_value": 0.0, "equity": float(initial_capital), "total_return": 0.0,
            "max_drawdown": 0.0, "peak_equity": float(initial_capital), "last_rebalanced_at": None,
            "funds": [{"symbol": item["symbol"], "name": item.get("name", item["symbol"])} for item in instruments],
            "commission_rate": self._settings.fund.commission_rate,
            "minimum_commission": self._settings.fund.minimum_commission,
            "stamp_duty_rate": self._settings.fund.stamp_duty_rate,
            "slippage_rate": self._settings.fund.slippage_rate,
            "subscription_fee_rate": self._settings.fund.subscription_fee_rate,
            "redemption_fee_rate": self._settings.fund.redemption_fee_rate,
            "ma_period": 60, "breakout_period": 20, "atr_period": 20, "atr_multiplier": 2.0,
            "short_ma_period": 5, "long_ma_period": 20, "momentum_period_days": 20,
            "trend_ma_period": 60, "max_positions": min(3, len(normalized)), "rebalance_interval_days": 7,
            "risk_lookback_days": 60,
        }
        payload.update({key: value for key, value in param_values.items() if key in _PARAM_COLUMNS})
        session_id = str(uuid.uuid4())
        self._repository.create_session(session_id, payload)
        self._wake_event.set()
        return session_id

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._repository.list_sessions()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._repository.get_session(session_id)

    def stop_session(self, session_id: str) -> bool:
        stopped = self._repository.stop_session(session_id, _china_time(self._clock()))
        if stopped:
            self._wake_event.set()
        return stopped

    def delete_session(self, session_id: str) -> bool:
        """委托仓储在 SQL 事务中校验状态并删除已停止任务。"""

        return self._repository.delete_session(session_id)

    def start(self) -> None:
        """取消停机期间错过执行日的净值订单，并恢复 SQL 中仍在运行的任务。"""

        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._repository.cancel_stale_orders_on_start()
            self._thread = threading.Thread(target=self._run, name="fund-paper-worker", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop_event.set()
            self._wake_event.set()
        if thread and thread.is_alive():
            thread.join(timeout=max(5, self._settings.market_data.request_timeout_seconds + 2))

    def _run(self) -> None:
        interval = max(1, self._settings.fund.worker_interval_seconds)
        while not self._stop_event.is_set():
            try:
                self.run_cycle()
            except Exception:
                logger.exception("基金实验 worker 本轮失败")
            self._wake_event.wait(interval)
            self._wake_event.clear()

    def run_cycle(self) -> None:
        """处理每个 SQL 运行任务；单任务行情故障只记录提示并等待下轮重试。"""

        for session in self._repository.get_running_sessions():
            if self._stop_event.is_set():
                return
            try:
                self._process_session(session)
            except Exception as exc:
                logger.exception("基金实验任务处理失败，session_id=%s", session.get("session_id"))
                try:
                    self._repository.set_session_message(str(session["session_id"]), f"本轮暂未更新：{type(exc).__name__}")
                except Exception:
                    logger.exception("记录基金实验任务提示失败")

    def _process_session(self, session: dict[str, Any]) -> None:
        kind, session_id = session["fund_type"], str(session["session_id"])
        now = _china_time(self._clock())
        today = now.date()
        exchange_open = _is_exchange_open(now)
        states = {item["symbol"]: dict(item) for item in session["funds"]}
        order_history = list(session.get("orders", []))
        orders: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        cash = float(session["cash"])
        initial_capital = float(session["initial_capital"])
        market_data: dict[str, dict[str, Any]] = {}

        for symbol, position in states.items():
            if kind == "open_end":
                points = self._market.get_nav(kind, symbol)
                if not points:
                    continue
                quote = _plain(self._market.get_quote(kind, symbol))
                nav = points[-1]
                price = float(nav.unit_nav)
                # 用累计净值计算总回报信号，缺失时退回单位净值；实际持仓仍按单位净值估值与成交。
                signal_values = [(point, float(point.accumulated_nav or point.unit_nav)) for point in points]
                bars = [FundBar(point.trade_date, value, value, value, value)
                        for point, value in signal_values if point.trade_date < nav.trade_date]
                signal_price = signal_values[-1][1]
                signal_bars = bars + [FundBar(nav.trade_date, signal_price, signal_price, signal_price, signal_price)]
                market_data[symbol] = {"price": price, "date": nav.trade_date, "bars": bars,
                                       "signal_bars": signal_bars,
                                       "points": points, "quote_at": now, "stale": bool(quote.get("stale", False)),
                                       "subscription_status": str(quote.get("subscription_status", "未知")),
                                       "redemption_status": str(quote.get("redemption_status", "未知"))}
            else:
                quote = self._market.get_quote(kind, symbol)
                price = float(quote.latest_price)
                bars = self._market.get_bars(kind, symbol, today - timedelta(days=500), today)
                bars = [bar for bar in bars if bar.trade_date < today]
                market_data[symbol] = {"price": price, "date": today, "bars": bars,
                                       "signal_bars": bars + [FundBar(today, price, price, price, price)],
                                       "quote_at": quote.fetched_at, "stale": bool(quote.stale),
                                       "quote": quote}
            position["last_price"] = price
            position["last_quote_at"] = market_data[symbol]["quote_at"]

        # 净值型订单只能在出现晚于信号日的已公布净值后成交。
        if kind == "open_end":
            pending_by_symbol = {symbol: [] for symbol in states}
            for order in order_history:
                if order.get("status") == "pending" and order.get("order_mode") == "nav_next":
                    pending_by_symbol.setdefault(order["symbol"], []).append(order)
            pending_orders = [(symbol, order) for symbol, items in pending_by_symbol.items() for order in items]
            # 同一净值日先执行赎回，再用回笼现金申购，降低等权调仓时现金不足的概率。
            pending_orders.sort(key=lambda item: (item[1]["side"] == "buy", item[1]["signal_date"], item[0]))
            for symbol, old_order in pending_orders:
                data = market_data.get(symbol)
                if not data:
                    continue
                candidates = [item for item in data["points"] if item.trade_date > old_order["signal_date"]]
                if not candidates:
                    continue
                execution_nav = candidates[0]
                position = states[symbol]
                side = old_order["side"]
                status_key = "subscription_status" if side == "buy" else "redemption_status"
                status_phrase = "开放申购" if side == "buy" else "开放赎回"
                if data["stale"] or status_phrase not in data[status_key]:
                    orders.append(dict(old_order, status="cancelled", execution_date=execution_nav.trade_date,
                                       reason="成交日基金暂停申购/赎回或状态不可确认"))
                    continue
                amount = min(float(old_order.get("amount", 0)), cash) if side == "buy" else 0
                cash, new_shares, filled = execute_open_nav_trade(
                    side, execution_nav.unit_nav, cash=cash, shares=float(position["shares"]),
                    amount=amount if side == "buy" else float(old_order.get("quantity", position["shares"])),
                    fee_rate=float(session["subscription_fee_rate"] if side == "buy" else session["redemption_fee_rate"]),
                )
                order = dict(old_order, status="filled", execution_date=execution_nav.trade_date)
                if filled:
                    order.update(amount=filled["amount"], quantity=filled["quantity"], execution_price=filled["execution_price"], fee=filled["fee"])
                    self._apply_position_trade(position, side, filled, execution_nav.trade_date)
                    trades.append(self._trade(session_id, kind, symbol, order, filled, execution_nav.trade_date))
                else:
                    order["status"] = "cancelled"
                orders.append(order)

        # 开放式基金现金分红使用唯一事件键，重启/重复轮询不会重复入账。
        if kind == "open_end":
            known_keys = {item.get("idempotency_key") for item in order_history}
            order_state = {item.get("idempotency_key"): item for item in order_history}
            order_state.update({item.get("idempotency_key"): item for item in orders})
            started_at = session.get("started_at")
            experiment_start = started_at.date() if isinstance(started_at, datetime) else started_at
            for symbol, position in states.items():
                if float(position["shares"]) <= 0 or symbol not in market_data:
                    continue
                for distribution in self._market.get_distributions(kind, symbol):
                    if distribution.ex_date > market_data[symbol]["date"] or (experiment_start and distribution.ex_date < experiment_start):
                        continue
                    source_hash = hashlib.sha256(distribution.source_key.encode("utf-8")).hexdigest()
                    key = f"{session_id}|{symbol}|dividend|{source_hash}"
                    if key in known_keys:
                        continue
                    # 按除息日前已成交的实验订单回放份额，避免把历史分红补发给新任务或除息日后买入者。
                    shares_on_record_date = 0.0
                    for filled in order_state.values():
                        execution_date = filled.get("execution_date")
                        if filled.get("symbol") != symbol or filled.get("status") != "filled" or execution_date is None or execution_date >= distribution.ex_date:
                            continue
                        if filled.get("side") == "buy":
                            shares_on_record_date += float(filled.get("quantity", 0))
                        elif filled.get("side") == "sell":
                            shares_on_record_date = max(0.0, shares_on_record_date - float(filled.get("quantity", 0)))
                    if shares_on_record_date <= 0:
                        continue
                    amount = shares_on_record_date * float(distribution.cash_per_unit)
                    cash += amount
                    item = {"idempotency_key": key, "fund_type": kind, "symbol": symbol, "side": "dividend",
                            "order_mode": "cash_event", "status": "filled", "signal_date": distribution.ex_date,
                            "execution_date": distribution.ex_date, "amount": amount, "quantity": shares_on_record_date,
                            "execution_price": market_data[symbol]["price"], "fee": 0.0,
                            "reason": "基金现金分红"}
                    orders.append(item)
                    trades.append({"idempotency_key": key, "fund_type": kind, "symbol": symbol,
                                   "trade_date": distribution.ex_date, "signal_date": distribution.ex_date,
                                   "side": "dividend", "execution_price": market_data[symbol]["price"],
                                   "quantity": shares_on_record_date, "notional": amount, "fee": 0.0,
                                   "reason": "基金现金分红"})
                    known_keys.add(key)

        # 旧策略沿用原订单路径，新基金组合策略按到期目标权重独立调仓。
        due_rotation = self._rotation_due(session, now)
        rotation_selected: list[str] | None = None
        if session["strategy_code"] == "momentum_rotation":
            bars_by_symbol = {symbol: data["bars"] + [FundBar(data["date"], data["price"], data["price"], data["price"], data["price"])]
                              for symbol, data in market_data.items()}
            rotation_selected = rank_momentum_rotation(
                bars_by_symbol, int(session["momentum_period_days"]), int(session["trend_ma_period"]), int(session["max_positions"]),
            ) if len(bars_by_symbol) == len(states) else None
            if rotation_selected is not None and due_rotation:
                if kind == "open_end":
                    ready = self._queue_open_rotation(session, states, market_data, rotation_selected, cash, order_history, orders)
                    if ready:
                        session["last_rebalanced_at"] = now
                elif exchange_open and all(not item["stale"] for item in market_data.values()):
                    cash = self._rebalance_exchange(session, states, market_data, rotation_selected, cash, orders, trades, now)
                    session["last_rebalanced_at"] = now

        elif session["strategy_code"] in FUND_PORTFOLIO_STRATEGY_CODES:
            target_weights: dict[str, float] | None = None
            if len(market_data) == len(states):
                bars_by_symbol = {symbol: data["signal_bars"] for symbol, data in market_data.items()}
                target_weights = portfolio_target_weights(session["strategy_code"], bars_by_symbol, session)
            if target_weights is not None and due_rotation:
                if kind == "open_end":
                    ready = self._queue_open_weighted_rebalance(
                        session, states, market_data, target_weights, cash, order_history, orders,
                    )
                    if ready:
                        session["last_rebalanced_at"] = now
                elif exchange_open and all(not item["stale"] for item in market_data.values()):
                    cash = self._rebalance_exchange_weights(session, states, market_data, target_weights, cash, orders, trades, now)
                    session["last_rebalanced_at"] = now

        elif kind == "open_end":
            self._queue_open_signals(session, states, market_data, order_history, orders, initial_capital, cash)
        elif exchange_open:
            cash = self._execute_exchange_signals(session, states, market_data, cash, orders, trades, initial_capital, now)

        equity_value = 0.0
        for symbol, position in states.items():
            data = market_data.get(symbol)
            if data:
                equity_value += float(position["shares"]) * float(data["price"])
        equity = cash + equity_value
        peak = max(float(session.get("peak_equity", initial_capital)), equity)
        drawdown = 1 - equity / peak if peak > 0 else 0
        account = {
            "cash": cash, "market_value": equity_value, "equity": equity,
            "total_return": equity / initial_capital - 1 if initial_capital else 0,
            "max_drawdown": max(float(session.get("max_drawdown", 0)), drawdown),
            "peak_equity": peak, "last_rebalanced_at": session.get("last_rebalanced_at"),
        }
        fund_updates = [{"fund_type": kind, **item} for item in states.values()]
        point = {"trade_date": today, "cash": cash, "market_value": equity_value, "equity": equity, "drawdown": drawdown}
        if not self._stop_event.is_set():
            self._repository.apply_fund_update(session_id, now, account, fund_updates, orders, trades, point)

    @staticmethod
    def _rotation_due(session: dict[str, Any], now: datetime) -> bool:
        last = session.get("last_rebalanced_at")
        if last is None:
            return True
        if isinstance(last, date) and not isinstance(last, datetime):
            last = datetime.combine(last, time.min, tzinfo=_CHINA_TZ)
        return (_china_time(now) - _china_time(last)).days >= int(session["rebalance_interval_days"])

    @staticmethod
    def _apply_position_trade(position: dict[str, Any], side: str, trade: dict[str, Any], trade_date: date, atr: float | None = None) -> None:
        quantity = float(trade["quantity"])
        if side == "buy":
            old_shares = float(position["shares"])
            position["shares"] = old_shares + quantity
            position["cost_basis"] = float(position.get("cost_basis", 0)) + float(trade["notional"]) + float(trade["fee"])
            position["entry_price"] = position["cost_basis"] / position["shares"] if position["shares"] else trade["execution_price"]
            position["entry_date"] = position.get("entry_date") or trade_date
            position["highest_price"] = max(float(position.get("highest_price") or 0), float(trade["execution_price"]))
            if atr is not None:
                position["entry_atr"] = atr
        elif side == "sell":
            old_shares = float(position["shares"])
            remaining = max(0.0, old_shares - quantity)
            position["cost_basis"] = float(position.get("cost_basis", 0)) * remaining / old_shares if old_shares else 0
            position["shares"] = remaining
            if remaining <= 0.00000001:
                position.update(shares=0.0, cost_basis=0.0, entry_date=None, entry_price=None, highest_price=None, entry_atr=None)

    @staticmethod
    def _trade(session_id: str, fund_type: str, symbol: str, order: dict[str, Any], details: dict[str, Any], trade_date: date) -> dict[str, Any]:
        return {"idempotency_key": order["idempotency_key"], "fund_type": fund_type, "symbol": symbol,
                "trade_date": trade_date, "signal_date": order["signal_date"], "side": order["side"],
                "execution_price": details["execution_price"], "quantity": details["quantity"],
                "notional": details["notional"], "fee": details["fee"], "reason": order["reason"]}

    @staticmethod
    def _new_order(session_id: str, fund_type: str, symbol: str, side: str, order_mode: str, signal_date: date,
                   status: str, amount: float, quantity: float, reason: str, execution_date: date | None = None,
                   execution_price: float | None = None, fee: float = 0.0) -> dict[str, Any]:
        return {"idempotency_key": f"{session_id}|{symbol}|{side}|{order_mode}|{signal_date.isoformat()}",
                "fund_type": fund_type, "symbol": symbol, "side": side, "order_mode": order_mode,
                "status": status, "signal_date": signal_date, "execution_date": execution_date,
                "amount": amount, "quantity": quantity, "execution_price": execution_price,
                "fee": fee, "reason": reason[:120]}

    def _queue_open_signals(self, session: dict[str, Any], states: dict[str, dict[str, Any]],
                            market_data: dict[str, dict[str, Any]], order_history: list[dict[str, Any]],
                            orders: list[dict[str, Any]], initial_capital: float, available_cash: float) -> None:
        pending_symbols = {order["symbol"] for order in order_history if order.get("status") == "pending"}
        budget = initial_capital / max(1, len(states))
        for symbol, position in states.items():
            data = market_data.get(symbol)
            if not data or data["date"] in {order.get("signal_date") for order in order_history if order.get("symbol") == symbol}:
                continue
            if data["stale"]:
                continue
            signal = strategy_signal(session["strategy_code"], data["bars"], data["price"], session)
            has_shares = float(position["shares"]) > 0
            side = "sell" if has_shares and signal["side"] == "sell" else "buy" if not has_shares and signal["side"] == "buy" else None
            if side == "buy" and "开放申购" not in data["subscription_status"]:
                side = None
            if side == "sell" and "开放赎回" not in data["redemption_status"]:
                side = None
            if side is None or symbol in pending_symbols:
                continue
            amount = min(available_cash, budget) if side == "buy" else 0.0
            quantity = 0.0 if side == "buy" else float(position["shares"])
            orders.append(self._new_order(str(session["session_id"]), "open_end", symbol, side, "nav_next", data["date"],
                                          "pending", amount, quantity, signal["reason"]))

    def _queue_open_rotation(self, session: dict[str, Any], states: dict[str, dict[str, Any]],
                             market_data: dict[str, dict[str, Any]], selected: list[str], cash: float,
                             order_history: list[dict[str, Any]], orders: list[dict[str, Any]]) -> bool:
        pending_symbols = {order["symbol"] for order in order_history if order.get("status") == "pending"}
        equity = cash + sum(float(position["shares"]) * float(market_data[symbol]["price"])
                            for symbol, position in states.items() if symbol in market_data)
        target = equity / len(selected) if selected else 0.0
        expected_sales = 0.0
        for symbol, position in states.items():
            data = market_data.get(symbol)
            if not data or data["stale"] or symbol in pending_symbols:
                continue
            held_value = float(position["shares"]) * float(data["price"])
            will_sell = float(position["shares"]) > 0 and (symbol not in selected or held_value > target * 1.02)
            if will_sell and "开放赎回" in data["redemption_status"]:
                shares_to_sell = float(position["shares"]) if symbol not in selected else max(0.0, (held_value - target) / float(data["price"]))
                expected_sales += shares_to_sell * float(data["price"]) * (1 - float(session["redemption_fee_rate"]))
        available_cash = cash + expected_sales
        ready = True
        for symbol, position in states.items():
            data = market_data.get(symbol)
            if not data:
                ready = False
                continue
            signal_date = data["date"]
            has_position = float(position["shares"]) > 0
            current_value = float(position["shares"]) * float(data["price"])
            should_sell = has_position and (symbol not in selected or current_value > target * 1.02)
            should_buy = symbol in selected and current_value < target * 0.98
            if data["stale"] or (should_buy and "开放申购" not in data["subscription_status"]) or (should_sell and "开放赎回" not in data["redemption_status"]):
                ready = False
                continue
            if symbol in pending_symbols or not (should_sell or should_buy):
                continue
            side = "sell" if should_sell else "buy"
            amount = min(available_cash, max(0.0, target - current_value)) if side == "buy" else 0.0
            if side == "buy":
                available_cash -= amount
            quantity = max(0.0, (current_value - target) / float(data["price"])) if symbol in selected and side == "sell" else float(position["shares"]) if side == "sell" else 0.0
            key = f"{session['session_id']}|{symbol}|{side}|nav_next|{signal_date.isoformat()}"
            if any(item.get("idempotency_key") == key for item in order_history):
                continue
            orders.append(self._new_order(str(session["session_id"]), "open_end", symbol,
                side, "nav_next", signal_date, "pending", amount, quantity,
                "组合动量轮动等权调仓"))
        return ready

    def _queue_open_weighted_rebalance(self, session: dict[str, Any], states: dict[str, dict[str, Any]],
                                       market_data: dict[str, dict[str, Any]], target_weights: dict[str, float],
                                       cash: float, order_history: list[dict[str, Any]],
                                       orders: list[dict[str, Any]]) -> bool:
        """为开放式基金按目标权重生成次日净值订单，并先估算赎回回款。"""

        pending_symbols = {order["symbol"] for order in order_history if order.get("status") == "pending"}
        equity = cash + sum(float(position["shares"]) * float(market_data[symbol]["price"])
                            for symbol, position in states.items() if symbol in market_data)
        target_values = {symbol: equity * max(0.0, float(target_weights.get(symbol, 0)))
                         for symbol in states}
        reason = self._fund_rebalance_reason(str(session["strategy_code"]))
        ready = True
        actions: list[tuple[str, str, float, float, dict[str, Any]]] = []
        expected_sales = 0.0
        existing_keys = {item.get("idempotency_key") for item in order_history}

        for symbol, position in states.items():
            data = market_data.get(symbol)
            if not data:
                ready = False
                continue
            if data["stale"]:
                ready = False
                continue
            current_value = float(position["shares"]) * float(data["price"])
            target = target_values[symbol]
            tolerance = target * 0.02
            side = "sell" if current_value > target + tolerance else "buy" if current_value < target - tolerance else ""
            if not side:
                continue
            if symbol in pending_symbols:
                ready = False
                continue
            if side == "sell" and "开放赎回" not in data["redemption_status"]:
                ready = False
                continue
            if side == "buy" and "开放申购" not in data["subscription_status"]:
                ready = False
                continue
            signal_date = data["date"]
            key = f"{session['session_id']}|{symbol}|{side}|nav_next|{signal_date.isoformat()}"
            if key in existing_keys:
                ready = False
                continue
            quantity = (float(position["shares"]) if target <= 0 else
                        min(float(position["shares"]), max(0.0, current_value - target) / float(data["price"]))) if side == "sell" else 0.0
            amount = max(0.0, target - current_value) if side == "buy" else 0.0
            actions.append((symbol, side, amount, quantity, data))
            if side == "sell":
                expected_sales += quantity * float(data["price"]) * (1 - float(session["redemption_fee_rate"]))

        # 组合中任一标的不可交易时不提交半套订单，等待下一轮有效净值再统一调仓。
        if not ready:
            return False

        # 按卖出、买入的顺序保留资金，避免申购订单超过本轮现金与预计赎回款。
        available_cash = cash + expected_sales
        actions.sort(key=lambda item: (item[1] == "buy", item[0]))
        for symbol, side, amount, quantity, data in actions:
            if side == "buy":
                amount = min(available_cash, amount)
                if amount <= 0.00001:
                    continue
                available_cash -= amount
            order = self._new_order(str(session["session_id"]), "open_end", symbol, side, "nav_next",
                                    data["date"], "pending", amount, quantity, reason)
            orders.append(order)
        return ready

    @staticmethod
    def _fund_rebalance_reason(strategy_code: str) -> str:
        """为成交记录保留使用的基金组合策略名称。"""

        return {
            "fund_equal_rebalance": "基金等权定期再平衡",
            "fund_low_volatility": "基金低波动风险配置",
            "fund_relative_strength": "基金相对强弱轮动",
        }.get(strategy_code, "基金组合调仓")

    def _rebalance_exchange_weights(self, session: dict[str, Any], states: dict[str, dict[str, Any]],
                                    market_data: dict[str, dict[str, Any]], target_weights: dict[str, float],
                                    cash: float, orders: list[dict[str, Any]], trades: list[dict[str, Any]],
                                    now: datetime) -> float:
        """场内基金先减持超配仓位，再用现金补足低配仓位。"""

        session_id, kind = str(session["session_id"]), str(session["fund_type"])
        equity = cash + sum(float(state["shares"]) * float(market_data[symbol]["price"])
                            for symbol, state in states.items() if symbol in market_data)
        target_values = {symbol: equity * max(0.0, float(target_weights.get(symbol, 0))) for symbol in states}
        reason = self._fund_rebalance_reason(str(session["strategy_code"]))

        for symbol, state in states.items():
            data = market_data.get(symbol)
            if not data or data["stale"]:
                continue
            held_value = float(state["shares"]) * float(data["price"])
            target = target_values[symbol]
            excess = held_value - target
            if float(state["shares"]) <= 0 or excess <= target * 0.02:
                continue
            quantity = float(state["shares"]) if target <= 0 else excess / float(data["price"])
            cash, _, details = execute_exchange_trade(
                "sell", data["price"], cash=cash, budget=excess, shares=float(state["shares"]),
                commission_rate=float(session["commission_rate"]), minimum_commission=float(session["minimum_commission"]),
                stamp_duty_rate=float(session["stamp_duty_rate"]), slippage_rate=float(session["slippage_rate"]), quantity=quantity,
            )
            if not details:
                continue
            order = self._new_order(session_id, kind, symbol, "sell", "market", now.date(), "filled",
                                    details["amount"], details["quantity"], reason, now.date(),
                                    details["execution_price"], details["fee"])
            self._apply_position_trade(state, "sell", details, now.date())
            orders.append(order)
            trades.append(self._trade(session_id, kind, symbol, order, details, now.date()))

        for symbol, state in states.items():
            data = market_data.get(symbol)
            if not data or data["stale"]:
                continue
            target = target_values[symbol]
            held_value = float(state["shares"]) * float(data["price"])
            desired = target - held_value
            if desired <= target * 0.02:
                continue
            cash, _, details = execute_exchange_trade(
                "buy", data["price"], cash=cash, budget=desired, shares=float(state["shares"]),
                commission_rate=float(session["commission_rate"]), minimum_commission=float(session["minimum_commission"]),
                stamp_duty_rate=float(session["stamp_duty_rate"]), slippage_rate=float(session["slippage_rate"]),
            )
            if not details:
                continue
            order = self._new_order(session_id, kind, symbol, "buy", "market", now.date(), "filled",
                                    details["amount"], details["quantity"], reason, now.date(),
                                    details["execution_price"], details["fee"])
            self._apply_position_trade(state, "buy", details, now.date())
            orders.append(order)
            trades.append(self._trade(session_id, kind, symbol, order, details, now.date()))
        return cash

    def _execute_exchange_signals(self, session: dict[str, Any], states: dict[str, dict[str, Any]],
                                  market_data: dict[str, dict[str, Any]], cash: float, orders: list[dict[str, Any]],
                                  trades: list[dict[str, Any]], initial_capital: float, now: datetime) -> float:
        session_id, kind = str(session["session_id"]), session["fund_type"]
        signal_items: list[tuple[str, str, dict[str, Any]]] = []
        for symbol, position in states.items():
            data = market_data.get(symbol)
            if not data or data["stale"] or data["price"] <= 0:
                continue
            signal = strategy_signal(session["strategy_code"], data["bars"], data["price"], session)
            held = float(position["shares"]) > 0
            if held and session["strategy_code"] == "breakout_ma_atr":
                position["highest_price"] = max(float(position.get("highest_price") or position.get("entry_price") or 0), data["price"])
                if position.get("entry_atr") and should_exit(data["price"], position["highest_price"], float(position["entry_atr"]), float(session["atr_multiplier"])):
                    signal["side"] = "sell"
                    signal["reason"] = "ATR 跟踪止损"
            side = signal["side"]
            if held and side == "sell":
                signal_items.append((symbol, side, signal))
            elif not held and side == "buy":
                signal_items.append((symbol, side, signal))
        # 先卖后买，卖出回笼的资金可用于本轮新买入。
        signal_items.sort(key=lambda item: (0 if item[1] == "sell" else 1, item[0]))
        budget = initial_capital / max(1, len(states))
        for symbol, side, signal in signal_items:
            data, position = market_data[symbol], states[symbol]
            result = execute_exchange_trade(side, data["price"], cash=cash, budget=budget if side == "buy" else cash,
                shares=float(position["shares"]), commission_rate=float(session["commission_rate"]),
                minimum_commission=float(session["minimum_commission"]), stamp_duty_rate=float(session["stamp_duty_rate"]),
                slippage_rate=float(session["slippage_rate"]))
            cash, _, details = result
            if not details:
                continue
            order = self._new_order(session_id, kind, symbol, side, "market", now.date(), "filled",
                details["amount"], details["quantity"], signal["reason"], now.date(), details["execution_price"], details["fee"])
            self._apply_position_trade(position, side, details, now.date(), signal.get("atr") if side == "buy" else None)
            orders.append(order)
            trades.append(self._trade(session_id, kind, symbol, order, details, now.date()))
        return cash

    def _rebalance_exchange(self, session: dict[str, Any], states: dict[str, dict[str, Any]],
                            market_data: dict[str, dict[str, Any]], selected: list[str], cash: float,
                            orders: list[dict[str, Any]], trades: list[dict[str, Any]], now: datetime) -> float:
        session_id, kind = str(session["session_id"]), session["fund_type"]
        equity = cash + sum(float(state["shares"]) * float(market_data[symbol]["price"])
                            for symbol, state in states.items() if symbol in market_data)
        target = equity / len(selected) if selected else 0.0
        for symbol, state in states.items():
            data = market_data.get(symbol)
            if not data or data["stale"]:
                continue
            held_value = float(state["shares"]) * data["price"]
            if float(state["shares"]) and (symbol not in selected or held_value > target * 1.02):
                budget = held_value if symbol not in selected else held_value - target
                new_cash, _, details = execute_exchange_trade("sell", data["price"], cash=cash, budget=budget,
                    shares=float(state["shares"]), commission_rate=float(session["commission_rate"]),
                    minimum_commission=float(session["minimum_commission"]), stamp_duty_rate=float(session["stamp_duty_rate"]),
                    slippage_rate=float(session["slippage_rate"]),
                    quantity=float(state["shares"]) if symbol not in selected else budget / data["price"])
                if details:
                    cash = new_cash
                    order = self._new_order(session_id, kind, symbol, "sell", "market", now.date(), "filled",
                        details["amount"], details["quantity"], "组合动量轮动等权调仓", now.date(), details["execution_price"], details["fee"])
                    self._apply_position_trade(state, "sell", details, now.date())
                    orders.append(order)
                    trades.append(self._trade(session_id, kind, symbol, order, details, now.date()))
        for symbol in selected:
            data, state = market_data.get(symbol), states.get(symbol)
            if not data or not state or data["stale"]:
                continue
            desired = max(0, target - float(state["shares"]) * data["price"])
            new_cash, _, details = execute_exchange_trade("buy", data["price"], cash=cash, budget=desired,
                shares=float(state["shares"]), commission_rate=float(session["commission_rate"]),
                minimum_commission=float(session["minimum_commission"]), stamp_duty_rate=float(session["stamp_duty_rate"]),
                slippage_rate=float(session["slippage_rate"]))
            if details:
                cash = new_cash
                order = self._new_order(session_id, kind, symbol, "buy", "market", now.date(), "filled",
                    details["amount"], details["quantity"], "组合动量轮动等权调仓", now.date(), details["execution_price"], details["fee"])
                self._apply_position_trade(state, "buy", details, now.date())
                orders.append(order)
                trades.append(self._trade(session_id, kind, symbol, order, details, now.date()))
        return cash
