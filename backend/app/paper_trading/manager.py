"""运行期纸面交易 worker；重启只接受启动后的新行情，不补做停机成交。"""

import logging
import threading
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from math import floor, isfinite
from threading import Thread
from typing import Callable
from uuid import uuid4

from app.config import Settings
from app.models import Quote
from app.paper_trading.strategy import (
    evaluate_defensive_mean_reversion, find_dual_ma_signal, find_entry_atr,
    get_strategy_catalog, rank_momentum_rotation, should_exit,
)

logger = logging.getLogger(__name__)
CHINA_TZ = timezone(timedelta(hours=8))
LOT_SIZE = 100


def _china_time(value: datetime) -> datetime:
    """将数据库无时区时间解释为北京时间，并统一 Quote 的时区。"""

    return value.replace(tzinfo=CHINA_TZ) if value.tzinfo is None else value.astimezone(CHINA_TZ)


def is_new_live_quote(
    quote: Quote,
    process_started_at: datetime,
    last_quote_at: datetime | None,
) -> bool:
    """拒绝过期行情、进程启动前的缓存和已处理过的同一份快照。"""

    if quote.stale or quote.latest_price <= 0:
        return False
    fetched_at = _china_time(quote.fetched_at)
    if fetched_at <= _china_time(process_started_at):
        return False
    return last_quote_at is None or fetched_at > _china_time(last_quote_at)


def _add_calendar_month(value: datetime) -> datetime:
    """按日历月计算实验到期时刻，月底日期自动落在目标月最后一天。"""

    month_index = value.month - 1 + 1
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _market_is_open(value: datetime) -> bool:
    """A 股纸面撮合只在工作日连续竞价时段读取行情并生成成交。"""

    local = _china_time(value)
    if local.weekday() >= 5:
        return False
    current = local.time().replace(tzinfo=None)
    return time(9, 30) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0)


def _commission(notional: float, rate: float, minimum: float) -> float:
    """按现有配置计算单边佣金，并遵守最低佣金设置。"""

    return max(minimum, notional * rate)


def _buy_quantity(budget: float, price: float, rate: float, minimum: float) -> int:
    """在股票预算和 100 股整数手限制内，预留买入佣金后取最大股数。"""

    quantity = floor(max(0.0, budget) / price / LOT_SIZE) * LOT_SIZE
    while quantity > 0:
        notional = quantity * price
        if notional + _commission(notional, rate, minimum) <= budget:
            return quantity
        quantity -= LOT_SIZE
    return 0


class PaperTradingManager:
    """负责会话创建、周期行情处理、持仓恢复和有序停止。"""

    def __init__(self, repository, market_data, settings: Settings, clock=None) -> None:
        self._repository = repository
        self._market_data = market_data
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(CHINA_TZ))
        self._process_started_at = _china_time(self._clock())
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._thread: Thread | None = None
        self._bars_cache: dict[tuple[str, date, int], list] = {}

    def start(self) -> None:
        """在数据库初始化完成后启动单 worker，并恢复运行中的持久会话。"""

        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._process_started_at = _china_time(self._clock())
            self._stop.clear()
            self._wake.clear()
            self._thread = Thread(target=self._run, name="paper-trading", daemon=False)
            self._thread.start()

    def create_session(self, initial_capital: float | None = None) -> str:
        """按配置股票池创建一个月会话；重复点击会返回当前运行中的会话。"""

        with self._lock:
            active = next((
                item for item in self._repository.get_active_paper_sessions()
                if item.get("task_name") == "一个月纸面实验" and item.get("ends_at") is not None
            ), None)
            if active:
                return active["session_id"]
            capital = float(initial_capital or self._settings.backtest.initial_capital)
            if initial_capital is not None and capital <= 0:
                raise ValueError("模拟初始资金必须大于 0")
            symbols = self._settings.paper_trading.symbols
            started_at = _china_time(self._clock())
            ends_at = _add_calendar_month(started_at)
            symbol_budgets = [(symbol, capital / len(symbols)) for symbol in symbols]
            for symbol in symbols:
                self._market_data.ensure_stock(symbol)
            session_id = str(uuid4())
            fees = self._settings.backtest
            strategy = self._settings.paper_trading
            self._repository.create_paper_session(
                session_id, started_at, ends_at, capital,
                fees.commission_rate, fees.minimum_commission,
                fees.stamp_duty_rate, fees.slippage_rate,
                strategy.ma_period, strategy.breakout_period,
                strategy.atr_period, strategy.atr_multiplier, symbol_budgets,
            )
            self._wake.set()
            return session_id

    def create_experiment(
        self,
        task_name: str,
        symbols: list[str],
        strategy_code: str,
        initial_capital: float,
        parameters: dict[str, int | float] | None = None,
    ) -> str:
        """验证策略参数，并将每项参数保存到 SQL Server 的独立字段。"""

        name = task_name.strip()
        normalized_symbols = [symbol.strip() for symbol in symbols]
        if not name or len(name) > 100:
            raise ValueError("任务名称长度必须为 1 到 100 个字符")
        if not 1 <= len(normalized_symbols) <= 20:
            raise ValueError("每个任务必须选择 1 到 20 只股票")
        if len(set(normalized_symbols)) != len(normalized_symbols):
            raise ValueError("股票代码不能重复")
        if any(len(symbol) != 6 or not symbol.isdigit() for symbol in normalized_symbols):
            raise ValueError("股票代码必须是六位数字")
        if initial_capital <= 0:
            raise ValueError("模拟初始资金必须大于 0")

        definitions = {item["code"]: item for item in self.get_strategies()}
        if strategy_code not in definitions:
            raise ValueError("未知的纸面实验策略")
        definition = definitions[strategy_code]
        parameter_definitions = definition.get("parameters", [])
        allowed_parameters = {item["key"]: item for item in parameter_definitions}
        supplied_parameters = parameters or {}
        unknown_parameters = set(supplied_parameters) - set(allowed_parameters)
        if unknown_parameters:
            raise ValueError("所选策略包含不支持的参数")

        resolved_parameters: dict[str, int | float] = {}
        for key, item in allowed_parameters.items():
            value = supplied_parameters.get(key, item["default"])
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f"{item['label']}必须是有效数字")
            if value < item["min"] or value > item["max"]:
                raise ValueError(f"{item['label']}必须在 {item['min']} 到 {item['max']} 之间")
            if item["type"] == "integer":
                if int(value) != value:
                    raise ValueError(f"{item['label']}必须是整数")
                resolved_parameters[key] = int(value)
            else:
                resolved_parameters[key] = float(value)

        if (
            "short_ma_period" in resolved_parameters
            and resolved_parameters["short_ma_period"] >= resolved_parameters["long_ma_period"]
        ):
            raise ValueError("短周期必须小于长周期")

        fees = self._settings.backtest
        configured = self._settings.paper_trading
        # 策略目录将用户输入限制在允许的字段中；其他存储列仍使用策略默认值。
        ma_period = int(resolved_parameters.get("ma_period", definition["ma_period"]))
        breakout_period = int(resolved_parameters.get("breakout_period", definition["breakout_period"]))
        atr_period = int(resolved_parameters.get("atr_period", definition["atr_period"]))
        atr_multiplier = float(resolved_parameters.get("atr_multiplier", definition["atr_multiplier"]))
        short_ma_period = int(resolved_parameters.get("short_ma_period", definition["short_ma_period"]))
        long_ma_period = int(resolved_parameters.get("long_ma_period", definition["long_ma_period"]))
        capital = float(initial_capital)
        # 组合轮动根据当期排名分配预算，创建时不预设成每股等额资金。
        symbol_budgets = [
            (symbol, 0.0 if strategy_code == "momentum_rotation" else capital / len(normalized_symbols))
            for symbol in normalized_symbols
        ]
        for symbol in normalized_symbols:
            self._market_data.ensure_stock(symbol)
        session_id = str(uuid4())
        self._repository.create_paper_session(
            session_id, _china_time(self._clock()), None, capital,
            fees.commission_rate, fees.minimum_commission, fees.stamp_duty_rate, fees.slippage_rate,
            ma_period, breakout_period, atr_period, atr_multiplier, symbol_budgets,
            task_name=name, strategy_code=strategy_code,
            short_ma_period=short_ma_period, long_ma_period=long_ma_period,
            momentum_period_days=int(resolved_parameters.get("momentum_period_days", 20)),
            trend_ma_period=int(resolved_parameters.get("trend_ma_period", 60)),
            max_positions=int(resolved_parameters.get("max_positions", 3)),
            rebalance_interval_days=int(resolved_parameters.get("rebalance_interval_days", 7)),
            max_holding_days=int(resolved_parameters.get("max_holding_days", definition.get("max_holding_days", 5))),
            cooldown_days=int(resolved_parameters.get("cooldown_days", definition.get("cooldown_days", 3))),
            target_annual_volatility=float(resolved_parameters.get(
                "target_annual_volatility", definition.get("target_annual_volatility", 0.15),
            )),
            max_position_ratio=float(resolved_parameters.get(
                "max_position_ratio", definition.get("max_position_ratio", 0.7),
            )),
        )
        self._wake.set()
        return session_id

    def get_strategies(self) -> list[dict[str, object]]:
        """让策略目录展示当前配置中的突破周期，同时保留双均线默认值。"""

        catalog = get_strategy_catalog()
        for item in catalog:
            if item["code"] == "breakout_ma_atr":
                configured = self._settings.paper_trading
                item.update({
                    "ma_period": configured.ma_period,
                    "breakout_period": configured.breakout_period,
                    "atr_period": configured.atr_period,
                    "atr_multiplier": configured.atr_multiplier,
                })
                defaults = {
                    "ma_period": configured.ma_period,
                    "breakout_period": configured.breakout_period,
                    "atr_period": configured.atr_period,
                    "atr_multiplier": configured.atr_multiplier,
                }
                for parameter in item["parameters"]:
                    if parameter["key"] in defaults:
                        parameter["default"] = defaults[parameter["key"]]
        return catalog

    def list_sessions(self):
        """返回所有运行与历史任务的 SQL 摘要。"""

        return self._repository.list_paper_sessions()

    def get_session(self, session_id: str):
        """读取单个任务及其持仓、净值和成交。"""

        return self._repository.get_paper_session(session_id)

    def stop_session(self, session_id: str) -> bool:
        """停止一个运行任务并唤醒 worker 重新检查其余任务。"""

        stopped = self._repository.stop_paper_session(session_id, _china_time(self._clock()))
        if stopped:
            self._wake.set()
        return stopped

    def get_current_session(self):
        """返回正在运行或最近结束的实验，供页面恢复显示。"""

        return self._repository.get_current_paper_session()

    def shutdown(self) -> None:
        """阻止新轮询并等待当前同步行情/数据库事务收尾后再退出。"""

        with self._lock:
            self._stop.set()
            self._wake.set()
            worker = self._thread
        if worker and worker is not threading.current_thread():
            worker.join()

    def run_cycle(self) -> None:
        """处理所有运行任务；每轮同一股票共用一份新行情，但账户逐任务隔离。"""

        sessions = self._repository.get_active_paper_sessions()
        if not sessions:
            return
        now = _china_time(self._clock())
        quote_cache: dict[str, Quote | Exception] = {}
        refresh_attempted = False

        def quote_for(symbol: str) -> Quote | Exception:
            nonlocal refresh_attempted
            if symbol not in quote_cache:
                force_refresh = not refresh_attempted
                refresh_attempted = True
                try:
                    quote_cache[symbol] = self._market_data.get_quote(symbol, force_refresh=force_refresh)
                except Exception as exc:
                    # 同一轮内复用异常结果，避免不同任务重复请求失败的行情。
                    quote_cache[symbol] = exc
            return quote_cache[symbol]

        for session in sessions:
            if self._stop.is_set():
                break
            session_id = session["session_id"]
            ends_at = session.get("ends_at")
            if ends_at is not None and now >= _china_time(ends_at):
                self._repository.update_paper_equity(session_id, now.date())
                self._repository.complete_paper_session(session_id, now)
                continue
            if not _market_is_open(now):
                continue

            if session["strategy_code"] == "momentum_rotation":
                try:
                    if self._run_rotation_session(session, quote_for):
                        self._repository.set_paper_session_error(session_id, None)
                except Exception:
                    logger.exception("组合轮动处理失败，session_id=%s", session_id)
                    self._repository.set_paper_session_error(session_id, "组合轮动处理失败；详见后端日志。")
                continue

            cash = float(session["cash"])
            changed = False
            processed_quote = False
            had_error = False
            expired = False
            for item in self._repository.get_paper_symbols(session_id):
                if self._stop.is_set():
                    break
                symbol = item["symbol"]
                try:
                    quote = quote_for(symbol)
                    if isinstance(quote, Exception):
                        raise quote
                    if self._stop.is_set():
                        break
                    checked_at = _china_time(self._clock())
                    if ends_at is not None and checked_at >= _china_time(ends_at):
                        # 行情请求可能跨过旧月度任务的到期时刻，必须在策略计算前结束任务。
                        self._repository.update_paper_equity(session_id, checked_at.date())
                        self._repository.complete_paper_session(session_id, checked_at)
                        expired = True
                        break
                    if not is_new_live_quote(quote, self._process_started_at, item["last_quote_at"]):
                        continue

                    quote_time = _china_time(quote.fetched_at)
                    last_price = float(quote.latest_price)
                    state = {
                        "shares": int(item["shares"] or 0),
                        "entry_date": item["entry_date"],
                        "entry_price": item["entry_price"],
                        "cost_basis": float(item["cost_basis"] or 0),
                        "highest_price": item["highest_price"],
                        "atr_value": item["atr_value"],
                        "last_price": last_price,
                        "last_exit_date": item.get("last_exit_date"),
                        "last_decision_date": item.get("last_decision_date"),
                    }
                    decision_date = None
                    if session["strategy_code"] == "defensive_mean_reversion":
                        # 防御策略只用前一日及更早的完整日线决定；本次新报价只用于撮合和估值。
                        signal_bars = [
                            bar for bar in self._history_before(
                                symbol, quote_time.date(), history_days=400,
                            ) if bar.trade_date < quote_time.date()
                        ]
                        latest_signal_date = signal_bars[-1].trade_date if signal_bars else None
                        previous_decision_date = state["last_decision_date"]
                        if latest_signal_date is not None and (
                            previous_decision_date is None or latest_signal_date > previous_decision_date
                        ):
                            decision_date = latest_signal_date
                            next_cash, trade = self._decide_trade(
                                session, item, state, cash, last_price, quote_time.date(), signal_bars,
                            )
                        else:
                            # 同一信号日的后续报价只刷新持仓估值，不再重复评估或下单。
                            next_cash, trade = cash, None
                            if state["shares"] > 0:
                                state["highest_price"] = max(
                                    float(state["highest_price"] or last_price), last_price,
                                )
                    else:
                        next_cash, trade = self._decide_trade(
                            session, item, state, cash, last_price, quote_time.date(),
                        )
                    if self._stop.is_set():
                        break
                    processed_at = _china_time(self._clock())
                    applied = self._repository.apply_paper_quote(
                        session_id, symbol, quote_time, state, next_cash, trade,
                        processed_at=processed_at, decision_date=decision_date,
                    )
                    if not applied and ends_at is not None and processed_at >= _china_time(ends_at):
                        # SQL 仓储在任务锁内已将跨过截止时刻的任务原子结束。
                        expired = True
                        break
                    if applied:
                        cash = next_cash
                        changed = True
                        processed_quote = True
                except Exception:
                    had_error = True
                    logger.exception("纸面交易处理股票行情失败，session_id=%s symbol=%s", session_id, symbol)
                    self._repository.set_paper_session_error(session_id, f"{symbol} 行情处理失败；详见后端日志。")
            if expired:
                continue
            if changed:
                self._repository.update_paper_equity(session_id, now.date())
            if processed_quote and not had_error:
                self._repository.set_paper_session_error(session_id, None)

    def _run_rotation_session(
        self,
        session: dict,
        quote_for: Callable[[str], Quote | Exception],
    ) -> bool:
        """收齐所有新报价后，以完整日线决定并原子保存组合操作。"""

        session_id = session["session_id"]
        items = self._repository.get_paper_symbols(session_id)
        if not items:
            return False

        quotes: dict[str, Quote] = {}
        for item in items:
            if self._stop.is_set():
                return False
            quote = quote_for(item["symbol"])
            if self._stop.is_set():
                return False
            if isinstance(quote, Exception):
                raise quote
            quote_time = _china_time(quote.fetched_at)
            if (
                not _market_is_open(quote_time)
                or not is_new_live_quote(quote, self._process_started_at, item["last_quote_at"])
            ):
                return False
            quotes[item["symbol"]] = quote

        processed_at = _china_time(self._clock())
        ends_at = session.get("ends_at")
        if ends_at is not None and processed_at >= _china_time(ends_at):
            self._repository.update_paper_equity(session_id, processed_at.date())
            self._repository.complete_paper_session(session_id, processed_at)
            return False
        if self._stop.is_set() or not _market_is_open(processed_at):
            return False

        last_rebalanced_at = session.get("last_rebalanced_at")
        interval = timedelta(days=int(session["rebalance_interval_days"]))
        rebalance_due = (
            last_rebalanced_at is None
            or processed_at - _china_time(last_rebalanced_at) >= interval
        )
        selected: list[str] = []
        if rebalance_due:
            bars_by_symbol = {}
            for item in items:
                if self._stop.is_set():
                    return False
                # 查询边界在报价日之前，避免盘中尚未收盘的日线参与动量排名。
                quote_date = _china_time(quotes[item["symbol"]].fetched_at).date()
                bars = self._history_before(item["symbol"], quote_date)
                bars_by_symbol[item["symbol"]] = [bar for bar in bars if bar.trade_date < quote_date]
            if self._stop.is_set():
                return False
            selected = rank_momentum_rotation(
                bars_by_symbol, int(session["momentum_period_days"]),
                int(session["trend_ma_period"]), int(session["max_positions"]),
            )
            if selected is None:
                # 缺少任何一只成分股的完整窗口时，留待下一轮重试且不写调仓时间。
                self._repository.set_paper_session_error(
                    session_id, "轮动策略历史日线不足，本轮未调仓；后续行情轮询会重试。",
                )
                return False

        updates, trades, cash = self._build_rotation_updates(
            session, items, quotes, selected, rebalance_due,
        )
        if self._stop.is_set():
            return False
        return self._repository.apply_paper_rebalance(
            session_id, processed_at, cash, updates, trades,
            processed_at if rebalance_due else None,
        )

    def _build_rotation_updates(
        self,
        session: dict,
        items: list[dict],
        quotes: dict[str, Quote],
        selected: list[str],
        rebalance_due: bool,
    ) -> tuple[list[dict], list[dict], float]:
        """先减持超配/落选股票，再用实际到账现金补足强势股目标仓位。"""

        cash = float(session["cash"])
        equity = cash + sum(
            int(item["shares"] or 0) * float(quotes[item["symbol"]].latest_price)
            for item in items
        )
        max_positions = int(session["max_positions"])
        target_budget = equity / max_positions
        slippage = float(session["slippage_rate"])
        commission_rate = float(session["commission_rate"])
        minimum_commission = float(session["minimum_commission"])
        stamp_duty_rate = float(session["stamp_duty_rate"])
        selected_symbols = set(selected)
        states: dict[str, dict] = {}
        trades: list[dict] = []

        for item in items:
            symbol = item["symbol"]
            quote = quotes[symbol]
            quote_time = _china_time(quote.fetched_at)
            price = float(quote.latest_price)
            state = {
                "symbol": symbol,
                "budget": target_budget if rebalance_due and symbol in selected_symbols else (
                    0.0 if rebalance_due else float(item["budget"])
                ),
                "shares": int(item["shares"] or 0),
                "entry_date": item["entry_date"],
                "entry_price": item["entry_price"],
                "cost_basis": float(item["cost_basis"] or 0),
                "highest_price": max(float(item["highest_price"] or price), price),
                "atr_value": item["atr_value"],
                "last_price": price,
                "last_quote_at": quote_time,
            }
            states[symbol] = state

        if rebalance_due:
            # 先处理减仓；因 T+1 暂时不能卖出的持仓仍保留在账户中。
            for item in items:
                symbol = item["symbol"]
                state = states[symbol]
                shares = state["shares"]
                if shares <= 0:
                    continue
                quote_time = state["last_quote_at"]
                entry_date = state["entry_date"]
                if isinstance(entry_date, datetime):
                    entry_date = entry_date.date()
                if entry_date is None or quote_time.date() <= entry_date:
                    continue

                target_shares = 0
                if symbol in selected_symbols:
                    target_shares = floor(target_budget / state["last_price"] / LOT_SIZE) * LOT_SIZE
                sell_quantity = floor(max(0, shares - target_shares) / LOT_SIZE) * LOT_SIZE
                if sell_quantity <= 0:
                    continue

                execution_price = state["last_price"] * (1 - slippage)
                notional = sell_quantity * execution_price
                commission = _commission(notional, commission_rate, minimum_commission)
                stamp_duty = notional * stamp_duty_rate
                proceeds = notional - commission - stamp_duty
                if cash + proceeds < 0:
                    # 不借款；现金不足支付最低卖出费用时保留该持仓，后续调仓再试。
                    continue
                remaining_shares = shares - sell_quantity
                state["cost_basis"] *= remaining_shares / shares
                state["shares"] = remaining_shares
                cash += proceeds
                if remaining_shares == 0:
                    state.update({
                        "entry_date": None, "entry_price": None, "cost_basis": 0,
                        "highest_price": None, "atr_value": None,
                    })
                trades.append({
                    "symbol": symbol, "quote_fetched_at": quote_time, "side": "sell",
                    "execution_price": execution_price, "quantity": sell_quantity,
                    "notional": notional, "commission": commission, "stamp_duty": stamp_duty,
                    "reason": "高于等权目标仓位" if symbol in selected_symbols else "跌出强弱轮动目标组合",
                })

            # 卖出资金到账后再买入；最低佣金、滑点和整手约束都计入可买预算。
            for symbol in selected:
                state = states[symbol]
                price = state["last_price"]
                current_value = state["shares"] * price
                deficit = max(0.0, target_budget - current_value)
                execution_price = price * (1 + slippage)
                quantity = _buy_quantity(
                    min(deficit, cash), execution_price, commission_rate, minimum_commission,
                )
                if quantity <= 0:
                    continue
                notional = quantity * execution_price
                commission = _commission(notional, commission_rate, minimum_commission)
                previous_shares = state["shares"]
                total_shares = previous_shares + quantity
                state["entry_price"] = (
                    (previous_shares * float(state["entry_price"] or price) + notional) / total_shares
                )
                state["entry_date"] = state["last_quote_at"].date()
                state["cost_basis"] += notional + commission
                state["shares"] = total_shares
                state["highest_price"] = max(float(state["highest_price"] or price), price)
                cash -= notional + commission
                trades.append({
                    "symbol": symbol, "quote_fetched_at": state["last_quote_at"], "side": "buy",
                    "execution_price": execution_price, "quantity": quantity, "notional": notional,
                    "commission": commission, "stamp_duty": 0, "reason": "按动量排名买入目标股票",
                })

        return list(states.values()), trades, cash

    def _decide_trade(
        self,
        session: dict,
        item: dict,
        state: dict,
        cash: float,
        last_price: float,
        trade_date: date,
        signal_bars: list | None = None,
    ) -> tuple[float, dict | None]:
        """按会话持久化的策略周期与费用生成单只股票的成交候选。"""

        strategy_code = session["strategy_code"]
        if strategy_code not in ("breakout_ma_atr", "dual_ma", "defensive_mean_reversion"):
            raise ValueError("任务包含未知策略代码")
        commission_rate = float(session["commission_rate"])
        minimum_commission = float(session["minimum_commission"])
        stamp_duty_rate = float(session["stamp_duty_rate"])
        slippage_rate = float(session["slippage_rate"])
        entry_date = state["entry_date"]
        next_cash = cash
        trade = None

        if strategy_code == "defensive_mean_reversion":
            if not signal_bars:
                return next_cash, None
            decision = evaluate_defensive_mean_reversion(
                signal_bars, state["entry_date"], state["entry_price"], state["atr_value"],
                state["last_exit_date"], int(session["trend_ma_period"]),
                int(session["atr_period"]), float(session["atr_multiplier"]),
                int(session["max_holding_days"]), int(session["cooldown_days"]),
                float(session["target_annual_volatility"]), float(session["max_position_ratio"]),
            )
            if state["shares"] > 0:
                if decision.action != "sell" or entry_date is None or trade_date <= entry_date:
                    return next_cash, None
                execution_price = last_price * (1 - slippage_rate)
                notional = state["shares"] * execution_price
                commission = _commission(notional, commission_rate, minimum_commission)
                stamp_duty = notional * stamp_duty_rate
                next_cash += notional - commission - stamp_duty
                trade = {
                    "side": "sell", "quantity": state["shares"], "execution_price": execution_price,
                    "notional": notional, "commission": commission, "stamp_duty": stamp_duty,
                    "reason": decision.reason, "signal_date": signal_bars[-1].trade_date,
                }
                state.update({
                    "shares": 0, "entry_date": None, "entry_price": None, "cost_basis": 0,
                    "highest_price": None, "atr_value": None, "last_exit_date": trade_date,
                })
                return next_cash, trade

            if decision.action != "buy" or decision.position_ratio is None or decision.entry_atr is None:
                return next_cash, None
            execution_price = last_price * (1 + slippage_rate)
            # 各股票使用独立的等额上限；未用现金不会转投其他股票，仓位比例作用于可用上限。
            budget = min(float(item["budget"]), cash) * decision.position_ratio
            quantity = _buy_quantity(budget, execution_price, commission_rate, minimum_commission)
            if quantity <= 0:
                return next_cash, None
            notional = quantity * execution_price
            commission = _commission(notional, commission_rate, minimum_commission)
            next_cash -= notional + commission
            trade = {
                "side": "buy", "quantity": quantity, "execution_price": execution_price,
                "notional": notional, "commission": commission, "stamp_duty": 0,
                "reason": decision.reason, "signal_date": signal_bars[-1].trade_date,
            }
            state.update({
                "shares": quantity, "entry_date": trade_date, "entry_price": execution_price,
                "cost_basis": notional + commission, "highest_price": last_price,
                "atr_value": decision.entry_atr,
            })
            return next_cash, trade

        if state["shares"] > 0:
            highest = max(float(state["highest_price"] or last_price), last_price)
            state["highest_price"] = highest
            can_sell_today = entry_date is not None and trade_date > entry_date
            if strategy_code == "breakout_ma_atr":
                should_sell = can_sell_today and should_exit(
                    last_price, highest, float(state["atr_value"] or 0), float(session["atr_multiplier"]),
                )
                reason = "跌破 ATR 跟踪止损"
            else:
                bars = self._history_before(item["symbol"], trade_date)
                signal = find_dual_ma_signal(
                    bars, last_price, int(session["short_ma_period"]), int(session["long_ma_period"]),
                )
                should_sell = can_sell_today and signal == "sell"
                reason = "短均线低于长均线"
            if should_sell:
                execution_price = last_price * (1 - slippage_rate)
                notional = state["shares"] * execution_price
                commission = _commission(notional, commission_rate, minimum_commission)
                stamp_duty = notional * stamp_duty_rate
                next_cash += notional - commission - stamp_duty
                trade = {
                    "side": "sell", "quantity": state["shares"], "execution_price": execution_price,
                    "notional": notional, "commission": commission, "stamp_duty": stamp_duty,
                    "reason": reason,
                }
                state.update({
                    "shares": 0, "entry_date": None, "entry_price": None,
                    "cost_basis": 0, "highest_price": None, "atr_value": None,
                })
            return next_cash, trade

        bars = self._history_before(item["symbol"], trade_date)
        atr = None
        if strategy_code == "breakout_ma_atr":
            atr = find_entry_atr(
                bars, last_price, int(session["ma_period"]),
                int(session["breakout_period"]), int(session["atr_period"]),
            )
            should_buy = atr is not None
            reason = "站上趋势均线并突破前期高点"
        else:
            should_buy = find_dual_ma_signal(
                bars, last_price, int(session["short_ma_period"]), int(session["long_ma_period"]),
            ) == "buy"
            reason = "短均线上穿长均线"
        if not should_buy:
            return next_cash, None

        execution_price = last_price * (1 + slippage_rate)
        budget = min(float(item["budget"]), cash)
        quantity = _buy_quantity(budget, execution_price, commission_rate, minimum_commission)
        if not quantity:
            return next_cash, None
        notional = quantity * execution_price
        commission = _commission(notional, commission_rate, minimum_commission)
        next_cash -= notional + commission
        trade = {
            "side": "buy", "quantity": quantity, "execution_price": execution_price,
            "notional": notional, "commission": commission, "stamp_duty": 0,
            "reason": reason,
        }
        state.update({
            "shares": quantity, "entry_date": trade_date, "entry_price": execution_price,
            "cost_basis": notional + commission, "highest_price": last_price, "atr_value": atr,
        })
        return next_cash, trade

    def _history_before(self, symbol: str, current_date: date, history_days: int = 220):
        """按股票、日期和范围缓存历史，避免旧短窗口命中防御策略的长预热请求。"""

        cache_key = (symbol, current_date, history_days)
        if cache_key not in self._bars_cache:
            self._bars_cache[cache_key] = self._market_data.get_bars(
                symbol, current_date - timedelta(days=history_days), current_date - timedelta(days=1),
            )
        return self._bars_cache[cache_key]

    def _run(self) -> None:
        """立即检查一次，之后按配置行情刷新间隔轮询，关闭时不再开启新一轮。"""

        interval = self._settings.redis.refresh_interval_seconds
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception:
                logger.exception("纸面交易周期处理失败")
            self._wake.wait(interval)
            self._wake.clear()
