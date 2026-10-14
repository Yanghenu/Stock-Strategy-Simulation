"""SQL Server 数据访问；所有用户值通过 DB-API 参数绑定。"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import DatabaseSettings
from app.database import create_connection
from app.models import BacktestParams, BacktestResult, Bar, CorporateAction, WatchedStock

logger = logging.getLogger(__name__)
CHINA_TZ = timezone(timedelta(hours=8))


class SqlRepository:
    """封装 SQL Server 连接、事务和标准化日线的读写。"""

    def __init__(
        self,
        settings: DatabaseSettings,
        connection_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._connection_factory = connection_factory or (
            lambda: create_connection(settings)
        )
        self._clock = clock or (lambda: datetime.now(CHINA_TZ))

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        """每次操作独立取用连接；失败时回滚并始终释放连接。"""

        connection = self._connection_factory()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception("SQL Server 事务失败")
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """仅在配置好的数据库中创建应用表，不创建或删除数据库本身。"""

        schema_path = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"
        schema = schema_path.read_text(encoding="utf-8")
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(schema)
            finally:
                cursor.close()

    def list_watchlist(self) -> list[WatchedStock]:
        """按加入顺序读取 SQL Server 中的关注标的。"""

        sql = "SELECT symbol, name FROM dbo.market_watchlist ORDER BY added_at, symbol;"
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                return [WatchedStock(str(row[0]), str(row[1])) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def add_watchlist(self, symbol: str, name: str) -> None:
        """幂等添加关注项；重试请求只更新名称，不生成重复行。"""

        sql = """MERGE dbo.market_watchlist WITH (HOLDLOCK) AS target
USING (VALUES (?, ?)) AS source (symbol, name)
ON target.symbol = source.symbol
WHEN MATCHED THEN UPDATE SET name = source.name
WHEN NOT MATCHED THEN INSERT (symbol, name, added_at)
VALUES (source.symbol, source.name, SYSUTCDATETIME());"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (symbol, name))
            finally:
                cursor.close()

    def remove_watchlist(self, symbol: str) -> bool:
        """删除关注项；不存在时返回 False，方便 API 保持幂等。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("DELETE FROM dbo.market_watchlist WHERE symbol = ?;", (symbol,))
                return cursor.rowcount > 0
            finally:
                cursor.close()

    @staticmethod
    def _db_datetime(value: datetime) -> datetime:
        """DATETIME2 不带时区；统一保存北京时间墙上值，避免依赖服务主机时区。"""

        return value.astimezone(CHINA_TZ).replace(tzinfo=None) if value.tzinfo else value

    def create_paper_session(
        self,
        session_id: str,
        started_at: datetime,
        ends_at: datetime | None,
        initial_capital: float,
        commission_rate: float,
        minimum_commission: float,
        stamp_duty_rate: float,
        slippage_rate: float,
        ma_period: int,
        breakout_period: int,
        atr_period: int,
        atr_multiplier: float,
        symbol_budgets: list[tuple[str, float]],
        task_name: str = "一个月纸面实验",
        strategy_code: str = "breakout_ma_atr",
        short_ma_period: int = 5,
        long_ma_period: int = 20,
        momentum_period_days: int = 20,
        trend_ma_period: int = 60,
        max_positions: int = 3,
        rebalance_interval_days: int = 7,
        max_holding_days: int = 5,
        cooldown_days: int = 3,
        target_annual_volatility: float = 0.15,
        max_position_ratio: float = 0.7,
    ) -> None:
        """在一个事务中创建任务及逐股票预算；旧月度调用保留默认字段。"""

        # status 使用固定值 running，其余策略列都通过参数绑定，避免字段顺序错位。
        # 防御型参数有独立列和兼容默认值，旧任务创建调用不需要变更。
        session_sql = """INSERT INTO dbo.paper_sessions
    (session_id, task_name, strategy_code, status, started_at, ends_at, initial_capital, cash,
     commission_rate, minimum_commission, stamp_duty_rate, slippage_rate,
     ma_period, breakout_period, atr_period, atr_multiplier, short_ma_period, long_ma_period,
     momentum_period_days, trend_ma_period, max_positions, rebalance_interval_days,
     max_holding_days, cooldown_days, target_annual_volatility, max_position_ratio)
VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"""
        symbol_sql = """INSERT INTO dbo.paper_session_symbols (session_id, symbol, budget)
VALUES (?, ?, ?);"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    session_sql,
                    (
                        session_id, task_name, strategy_code, self._db_datetime(started_at),
                        self._db_datetime(ends_at) if ends_at else None,
                        float(initial_capital), float(initial_capital), float(commission_rate),
                        float(minimum_commission), float(stamp_duty_rate), float(slippage_rate),
                        ma_period, breakout_period, atr_period, float(atr_multiplier),
                        short_ma_period, long_ma_period, momentum_period_days, trend_ma_period,
                        max_positions, rebalance_interval_days,
                        max_holding_days, cooldown_days, float(target_annual_volatility), float(max_position_ratio),
                    ),
                )
                cursor.executemany(
                    symbol_sql,
                    [(session_id, symbol, float(budget)) for symbol, budget in symbol_budgets],
                )
                # 建立起始净值点，使页面从实验首日就能画出可对照的账户曲线。
                cursor.execute(
                    """INSERT INTO dbo.paper_equity
    (session_id, trade_date, cash, market_value, equity, drawdown)
VALUES (?, ?, ?, 0, ?, 0);""",
                    (session_id, self._db_datetime(started_at).date(), float(initial_capital), float(initial_capital)),
                )
            finally:
                cursor.close()

    def get_active_paper_sessions(self) -> list[dict[str, Any]]:
        """读取全部运行任务，服务重启后由单 worker 逐个恢复。"""

        sql = """SELECT session_id, task_name, strategy_code, started_at, ends_at, initial_capital, cash,
    commission_rate, minimum_commission, stamp_duty_rate, slippage_rate,
    ma_period, breakout_period, atr_period, atr_multiplier, short_ma_period, long_ma_period,
    momentum_period_days, trend_ma_period, max_positions, rebalance_interval_days, last_rebalanced_at,
    max_holding_days, cooldown_days, target_annual_volatility, max_position_ratio
FROM dbo.paper_sessions WHERE status='running' ORDER BY started_at, session_id;"""
        names = (
            "session_id", "task_name", "strategy_code", "started_at", "ends_at", "initial_capital", "cash",
            "commission_rate", "minimum_commission", "stamp_duty_rate", "slippage_rate",
            "ma_period", "breakout_period", "atr_period", "atr_multiplier", "short_ma_period", "long_ma_period",
            "momentum_period_days", "trend_ma_period", "max_positions", "rebalance_interval_days",
            "last_rebalanced_at", "max_holding_days", "cooldown_days",
            "target_annual_volatility", "max_position_ratio",
        )
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                rows = cursor.fetchall()
            finally:
                cursor.close()
        sessions = [dict(zip(names, row)) for row in rows]
        for session in sessions:
            session["session_id"] = str(session["session_id"])
        return sessions

    def get_active_paper_session(self) -> dict[str, Any] | None:
        """兼容旧调用，返回最近启动的运行任务。"""

        sessions = self.get_active_paper_sessions()
        return sessions[-1] if sessions else None

    def list_paper_sessions(self) -> list[dict[str, Any]]:
        """用单条聚合查询返回任务摘要，避免任务列表逐项读取账本。"""

        sql = """SELECT p.session_id, p.task_name, p.strategy_code, p.status, p.started_at,
    p.ends_at, p.completed_at, p.initial_capital, p.cash, holdings.market_value,
    p.cash + holdings.market_value AS equity,
    CASE WHEN p.initial_capital=0 THEN 0
         ELSE (p.cash + holdings.market_value) / p.initial_capital - 1 END AS total_return,
    COALESCE(curve.max_drawdown, 0) AS max_drawdown, holdings.symbol_count, p.error_message
FROM dbo.paper_sessions AS p
OUTER APPLY (
    SELECT COUNT(*) AS symbol_count,
        COALESCE(SUM(shares * COALESCE(last_price, 0)), 0) AS market_value
    FROM dbo.paper_session_symbols WHERE session_id=p.session_id
) AS holdings
OUTER APPLY (
    SELECT MIN(drawdown) AS max_drawdown
    FROM dbo.paper_equity WHERE session_id=p.session_id
) AS curve
ORDER BY p.started_at DESC, p.session_id;"""
        names = (
            "session_id", "task_name", "strategy_code", "status", "started_at", "ends_at", "completed_at",
            "initial_capital", "cash", "market_value", "equity", "total_return", "max_drawdown",
            "symbol_count", "error_message",
        )
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                rows = cursor.fetchall()
            finally:
                cursor.close()
        sessions = [dict(zip(names, row)) for row in rows]
        for session in sessions:
            session["session_id"] = str(session["session_id"])
        return sessions

    def stop_paper_session(self, session_id: str, stopped_at: datetime) -> bool:
        """只停止运行中的任务，并保留其成交、持仓與净值记录。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """UPDATE dbo.paper_sessions SET status='stopped', completed_at=?, error_message=NULL
WHERE session_id=? AND status='running';""",
                    (self._db_datetime(stopped_at), session_id),
                )
                return cursor.rowcount > 0
            finally:
                cursor.close()

    def get_paper_symbols(self, session_id: str) -> list[dict[str, Any]]:
        """读取实验股票、已持仓数量及最近一次成交行情时间。"""

        sql = """SELECT s.symbol, COALESCE(k.name, s.symbol), s.budget, s.shares,
    s.entry_date, s.entry_price, s.cost_basis, s.highest_price, s.atr_value,
    s.last_price, s.last_quote_at, s.last_exit_date, s.last_decision_date
FROM dbo.paper_session_symbols AS s
LEFT JOIN dbo.stocks AS k ON k.symbol=s.symbol
WHERE s.session_id=? ORDER BY s.symbol;"""
        names = (
            "symbol", "name", "budget", "shares", "entry_date", "entry_price",
            "cost_basis", "highest_price", "atr_value", "last_price", "last_quote_at",
            "last_exit_date", "last_decision_date",
        )
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (session_id,))
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [dict(zip(names, row)) for row in rows]

    def apply_paper_quote(
        self,
        session_id: str,
        symbol: str,
        quote_fetched_at: datetime,
        state: dict[str, Any],
        cash: float,
        trade: dict[str, Any] | None,
        processed_at: datetime | None = None,
        decision_date: date | None = None,
    ) -> bool:
        """按报价时间幂等更新持仓、现金和成交，防止重启/重复轮询重复下单。"""

        quote_time = self._db_datetime(quote_fetched_at)
        # 保留信号决策时间，之后在任务行锁内再读一次时钟，处理锁等待跨过截止点的情况。
        process_time = self._db_datetime(processed_at) if processed_at else None
        select_sql = """SELECT last_quote_at, last_decision_date FROM dbo.paper_session_symbols WITH (UPDLOCK, HOLDLOCK)
WHERE session_id=? AND symbol=?;"""
        update_sql = """UPDATE dbo.paper_session_symbols SET shares=?, entry_date=?, entry_price=?,
    cost_basis=?, highest_price=?, atr_value=?, last_price=?, last_quote_at=?,
    last_exit_date=COALESCE(?, last_exit_date), last_decision_date=COALESCE(?, last_decision_date)
WHERE session_id=? AND symbol=? AND (last_quote_at IS NULL OR last_quote_at < ?);"""
        # 未提供日期时保留数据库既有值；新信号日即使没有成交也在持仓更新中原子落库。
        cash_sql = "UPDATE dbo.paper_sessions SET cash=? WHERE session_id=? AND status='running';"
        trade_sql = """INSERT INTO dbo.paper_trades
    (session_id, symbol, quote_fetched_at, side, execution_price, quantity,
     notional, commission, stamp_duty, reason, signal_date)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                # 对任务行加更新锁，使“停止任务”和“写入成交”在 SQL Server 中有明确先后次序。
                cursor.execute(
                    "SELECT status, ends_at FROM dbo.paper_sessions WITH (UPDLOCK, HOLDLOCK) WHERE session_id=?;",
                    (session_id,),
                )
                session = cursor.fetchone()
                if session is None or session[0] != "running":
                    return False
                locked_time = self._db_datetime(self._clock())
                effective_time = max(process_time, locked_time) if process_time else locked_time
                if session[1] is not None and effective_time >= session[1]:
                    # 在同一任务行锁内结束任务，确保到期与成交写入只有一个先发生。
                    cursor.execute(
                        "UPDATE dbo.paper_sessions SET status='completed', completed_at=? WHERE session_id=? AND status='running';",
                        (effective_time, session_id),
                    )
                    return False
                cursor.execute(select_sql, (session_id, symbol))
                row = cursor.fetchone()
                if row is None or (row[0] is not None and row[0] >= quote_time):
                    return False
                # 行锁内检查信号日；拒绝重复或过期决策，避免先修改现金/持仓后才防重。
                if decision_date is not None and row[1] is not None and decision_date <= row[1]:
                    return False
                cursor.execute(
                    update_sql,
                    (
                        int(state["shares"]), state["entry_date"], state["entry_price"],
                        float(state["cost_basis"]), state["highest_price"], state["atr_value"],
                        float(state["last_price"]), quote_time, state.get("last_exit_date"),
                        decision_date, session_id, symbol, quote_time,
                    ),
                )
                if cursor.rowcount <= 0:
                    return False
                cursor.execute(cash_sql, (float(cash), session_id))
                if trade is not None:
                    cursor.execute(
                        trade_sql,
                        (
                            session_id, symbol, quote_time, trade["side"],
                            float(trade["execution_price"]), int(trade["quantity"]),
                            float(trade["notional"]), float(trade["commission"]),
                            float(trade["stamp_duty"]), str(trade["reason"]), decision_date,
                        ),
                    )
                return True
            finally:
                cursor.close()

    def apply_paper_rebalance(
        self,
        session_id: str,
        processed_at: datetime,
        cash: float,
        symbol_updates: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        rebalanced_at: datetime | None,
    ) -> bool:
        """在单个事务中写入组合持仓、成交、现金、净值和可选调仓时间。"""

        if not symbol_updates or cash < 0:
            return False
        process_time = self._db_datetime(processed_at)
        quote_times = {
            item["symbol"]: self._db_datetime(item["last_quote_at"])
            for item in symbol_updates
        }
        if len(quote_times) != len(symbol_updates):
            raise ValueError("组合行情包含重复股票")

        session_sql = """SELECT status, ends_at, initial_capital
FROM dbo.paper_sessions WITH (UPDLOCK, HOLDLOCK) WHERE session_id=?;"""
        symbol_sql = """SELECT symbol, last_quote_at
FROM dbo.paper_session_symbols WITH (UPDLOCK, HOLDLOCK) WHERE session_id=?;"""
        # 组合调仓的旧策略不传日期；COALESCE 避免清空防御型策略已经保存的状态。
        update_sql = """UPDATE dbo.paper_session_symbols SET budget=?, shares=?, entry_date=?,
    entry_price=?, cost_basis=?, highest_price=?, atr_value=?, last_price=?, last_quote_at=?,
    last_exit_date=COALESCE(?, last_exit_date), last_decision_date=COALESCE(?, last_decision_date)
WHERE session_id=? AND symbol=? AND (last_quote_at IS NULL OR last_quote_at < ?);"""
        cash_sql = """UPDATE dbo.paper_sessions
SET cash=?, last_rebalanced_at=COALESCE(?, last_rebalanced_at)
WHERE session_id=? AND status='running';"""
        trade_sql = """INSERT INTO dbo.paper_trades
    (session_id, symbol, quote_fetched_at, side, execution_price, quantity,
     notional, commission, stamp_duty, reason, signal_date)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"""
        equity_sql = """MERGE dbo.paper_equity WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?, ?, ?, ?)) AS source (session_id, trade_date, cash, market_value, equity, drawdown)
ON target.session_id=source.session_id AND target.trade_date=source.trade_date
WHEN MATCHED THEN UPDATE SET cash=source.cash, market_value=source.market_value,
    equity=source.equity, drawdown=source.drawdown
WHEN NOT MATCHED THEN INSERT (session_id, trade_date, cash, market_value, equity, drawdown)
VALUES (source.session_id, source.trade_date, source.cash, source.market_value, source.equity, source.drawdown);"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                # 先锁住任务，再锁住全部成分股，确保停止任务或另一轮行情不会穿插半笔组合写入。
                cursor.execute(session_sql, (session_id,))
                session = cursor.fetchone()
                if session is None or session[0] != "running":
                    return False
                locked_time = self._db_datetime(self._clock())
                effective_time = max(process_time, locked_time)
                if session[1] is not None and effective_time >= session[1]:
                    cursor.execute(
                        "UPDATE dbo.paper_sessions SET status='completed', completed_at=? WHERE session_id=? AND status='running';",
                        (effective_time, session_id),
                    )
                    return False

                cursor.execute(symbol_sql, (session_id,))
                stored_quotes = dict(cursor.fetchall())
                if set(stored_quotes) != set(quote_times):
                    return False
                for symbol, quote_time in quote_times.items():
                    previous = stored_quotes[symbol]
                    if previous is not None and quote_time <= previous:
                        return False

                for item in symbol_updates:
                    quote_time = quote_times[item["symbol"]]
                    cursor.execute(
                        update_sql,
                        (
                            float(item["budget"]), int(item["shares"]), item["entry_date"], item["entry_price"],
                            float(item["cost_basis"]), item["highest_price"], item["atr_value"],
                            float(item["last_price"]), quote_time, item.get("last_exit_date"),
                            item.get("last_decision_date"), session_id, item["symbol"], quote_time,
                        ),
                    )
                    if cursor.rowcount <= 0:
                        raise RuntimeError(f"组合持仓更新失败：{item['symbol']}")

                for trade in trades:
                    symbol = trade["symbol"]
                    if symbol not in quote_times:
                        raise ValueError("组合成交股票不在当前任务股票列表中")
                    cursor.execute(
                        trade_sql,
                        (
                            session_id, symbol, self._db_datetime(trade["quote_fetched_at"]), trade["side"],
                            float(trade["execution_price"]), int(trade["quantity"]), float(trade["notional"]),
                            float(trade["commission"]), float(trade["stamp_duty"]), str(trade["reason"]),
                            trade.get("signal_date"),
                        ),
                    )

                normalized_rebalanced_at = self._db_datetime(rebalanced_at) if rebalanced_at else None
                cursor.execute(cash_sql, (float(cash), normalized_rebalanced_at, session_id))
                if cursor.rowcount <= 0:
                    raise RuntimeError("组合账户现金更新失败")

                market_value = sum(
                    int(item["shares"]) * float(item["last_price"]) for item in symbol_updates
                )
                equity = float(cash) + market_value
                cursor.execute(
                    "SELECT COALESCE(MAX(equity), ?) FROM dbo.paper_equity WHERE session_id=?;",
                    (float(session[2]), session_id),
                )
                peak = max(float(session[2]), float(cursor.fetchone()[0]))
                drawdown = equity / peak - 1 if peak else 0
                cursor.execute(
                    equity_sql,
                    (session_id, process_time.date(), float(cash), market_value, equity, drawdown),
                )
                return True
            finally:
                cursor.close()

    def update_paper_equity(self, session_id: str, trade_date: date) -> None:
        """将当日现金与持仓市值汇总为一条标准化净值/回撤记录。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT cash, initial_capital FROM dbo.paper_sessions WHERE session_id=? AND status='running';",
                    (session_id,),
                )
                session = cursor.fetchone()
                if session is None:
                    return
                cash, initial_capital = float(session[0]), float(session[1])
                cursor.execute(
                    """SELECT COALESCE(SUM(shares * COALESCE(last_price, 0)), 0)
FROM dbo.paper_session_symbols WHERE session_id=?;""",
                    (session_id,),
                )
                market_value = float(cursor.fetchone()[0])
                cursor.execute(
                    "SELECT COALESCE(MAX(equity), ?) FROM dbo.paper_equity WHERE session_id=?;",
                    (initial_capital, session_id),
                )
                peak = max(initial_capital, float(cursor.fetchone()[0]))
                equity = cash + market_value
                drawdown = equity / peak - 1 if peak else 0
                cursor.execute(
                    """MERGE dbo.paper_equity WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?, ?, ?, ?)) AS source (session_id, trade_date, cash, market_value, equity, drawdown)
ON target.session_id=source.session_id AND target.trade_date=source.trade_date
WHEN MATCHED THEN UPDATE SET cash=source.cash, market_value=source.market_value,
    equity=source.equity, drawdown=source.drawdown
WHEN NOT MATCHED THEN INSERT (session_id, trade_date, cash, market_value, equity, drawdown)
VALUES (source.session_id, source.trade_date, source.cash, source.market_value, source.equity, source.drawdown);""",
                    (session_id, trade_date, cash, market_value, equity, drawdown),
                )
            finally:
                cursor.close()

    def complete_paper_session(self, session_id: str, completed_at: datetime) -> None:
        """到期后停止 worker 下单；持仓按最后有效报价保留为浮动盈亏。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "UPDATE dbo.paper_sessions SET status='completed', completed_at=? WHERE session_id=? AND status='running';",
                    (self._db_datetime(completed_at), session_id),
                )
            finally:
                cursor.close()

    def set_paper_session_error(self, session_id: str, message: str | None) -> None:
        """记录可恢复行情错误；成功处理新行情后传 None 清除旧错误。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "UPDATE dbo.paper_sessions SET error_message=? WHERE session_id=? AND status='running';",
                    (message[:500] if message else None, session_id),
                )
            finally:
                cursor.close()

    def get_paper_session(self, session_id: str) -> dict[str, Any] | None:
        """读取会话概览、股票持仓、每日净值和逐笔成交。"""

        session_sql = """SELECT session_id, task_name, strategy_code, status, started_at, ends_at, completed_at,
    initial_capital, cash, ma_period, breakout_period, atr_period, atr_multiplier,
    commission_rate, minimum_commission, stamp_duty_rate, slippage_rate,
    short_ma_period, long_ma_period, momentum_period_days, trend_ma_period, max_positions,
    rebalance_interval_days, last_rebalanced_at, error_message,
    max_holding_days, cooldown_days, target_annual_volatility, max_position_ratio
FROM dbo.paper_sessions WHERE session_id=?;"""
        session_names = (
            "session_id", "task_name", "strategy_code", "status", "started_at", "ends_at", "completed_at",
            "initial_capital", "cash", "ma_period", "breakout_period", "atr_period", "atr_multiplier",
            "commission_rate", "minimum_commission", "stamp_duty_rate", "slippage_rate",
            "short_ma_period", "long_ma_period", "momentum_period_days", "trend_ma_period", "max_positions",
            "rebalance_interval_days", "last_rebalanced_at", "error_message",
            "max_holding_days", "cooldown_days", "target_annual_volatility", "max_position_ratio",
        )
        symbol_sql = """SELECT s.symbol, COALESCE(k.name, s.symbol), s.budget, s.shares,
    s.entry_date, s.entry_price, s.cost_basis, s.highest_price, s.atr_value, s.last_price, s.last_quote_at, s.last_exit_date, s.last_decision_date
FROM dbo.paper_session_symbols AS s LEFT JOIN dbo.stocks AS k ON k.symbol=s.symbol
WHERE s.session_id=? ORDER BY s.symbol;"""
        trade_sql = """SELECT symbol, quote_fetched_at, side, execution_price, quantity,
    notional, commission, stamp_duty, reason, signal_date
FROM dbo.paper_trades WHERE session_id=? ORDER BY quote_fetched_at, trade_id;"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(session_sql, (session_id,))
                row = cursor.fetchone()
                if row is None:
                    return None
                result = dict(zip(session_names, row))
                result["session_id"] = str(result["session_id"])
                cursor.execute(symbol_sql, (session_id,))
                symbol_rows = cursor.fetchall()
                symbol_names = (
                    "symbol", "name", "budget", "shares", "entry_date", "entry_price", "cost_basis",
                    "highest_price", "atr_value", "last_price", "last_quote_at",
                    "last_exit_date", "last_decision_date",
                )
                symbols = [dict(zip(symbol_names, item)) for item in symbol_rows]
                cursor.execute(
                    "SELECT trade_date, cash, market_value, equity, drawdown FROM dbo.paper_equity WHERE session_id=? ORDER BY trade_date;",
                    (session_id,),
                )
                curve_rows = cursor.fetchall()
                cursor.execute(trade_sql, (session_id,))
                trade_rows = cursor.fetchall()
            finally:
                cursor.close()
        curve_names = ("trade_date", "cash", "market_value", "equity", "drawdown")
        trade_names = (
            "symbol", "trade_date", "side", "price", "quantity", "notional",
            "commission", "stamp_duty", "reason", "signal_date",
        )
        result["symbols"] = symbols
        result["equity_curve"] = [dict(zip(curve_names, item)) for item in curve_rows]
        result["trades"] = [dict(zip(trade_names, item)) for item in trade_rows]
        result["market_value"] = sum(
            int(item["shares"]) * float(item["last_price"] or 0) for item in symbols
        )
        result["equity"] = float(result["cash"]) + result["market_value"]
        result["total_return"] = result["equity"] / float(result["initial_capital"]) - 1
        result["max_drawdown"] = min((float(item["drawdown"]) for item in result["equity_curve"]), default=0.0)
        return result

    def get_current_paper_session(self) -> dict[str, Any] | None:
        """优先返回运行中会话，否则返回最近结束的一次实验。"""

        active = self.get_active_paper_session()
        if active:
            return self.get_paper_session(active["session_id"])
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT TOP (1) session_id FROM dbo.paper_sessions ORDER BY started_at DESC;")
                row = cursor.fetchone()
            finally:
                cursor.close()
        return self.get_paper_session(str(row[0])) if row else None

    def upsert_stock(self, symbol: str, name: str, exchange: str | None = None) -> None:
        """保存股票基础信息，满足日线和权益事件表的外键约束。"""

        sql = """MERGE dbo.stocks WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?)) AS source (symbol, name, exchange)
ON target.symbol = source.symbol
WHEN MATCHED THEN UPDATE SET name=source.name, exchange=source.exchange,
    updated_at=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (symbol, name, exchange)
VALUES (source.symbol, source.name, source.exchange);"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (symbol, name, exchange))
            finally:
                cursor.close()

    def upsert_bars(self, symbol: str, bars: list[Bar]) -> None:
        """批量保存标准化日线；相同股票/交易日更新而不是重复插入。"""

        if not bars:
            return
        sql = """MERGE dbo.daily_bars WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)) AS source
    (symbol, trade_date, open_price, high_price, low_price, close_price,
     adj_open, adj_high, adj_low, adj_close, volume, turnover)
ON target.symbol = source.symbol AND target.trade_date = source.trade_date
WHEN MATCHED THEN UPDATE SET open_price=source.open_price, high_price=source.high_price,
    low_price=source.low_price, close_price=source.close_price, adj_open=source.adj_open,
    adj_high=source.adj_high, adj_low=source.adj_low, adj_close=source.adj_close,
    volume=source.volume, turnover=source.turnover
WHEN NOT MATCHED THEN INSERT
    (symbol, trade_date, open_price, high_price, low_price, close_price,
     adj_open, adj_high, adj_low, adj_close, volume, turnover)
VALUES (source.symbol, source.trade_date, source.open_price, source.high_price,
     source.low_price, source.close_price, source.adj_open, source.adj_high,
     source.adj_low, source.adj_close, source.volume, source.turnover);"""
        rows = [
            (
                symbol, item.trade_date, item.open, item.high, item.low, item.close,
                item.adj_open, item.adj_high, item.adj_low, item.adj_close,
                int(item.volume), item.turnover,
            )
            for item in bars
        ]
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.executemany(sql, rows)
            finally:
                cursor.close()

    def get_bars(self, symbol: str, start_date: date, end_date: date) -> list[Bar]:
        """按区间读取日线并映射回策略可直接使用的领域类型。"""

        sql = """SELECT trade_date, open_price, high_price, low_price, close_price,
    adj_open, adj_high, adj_low, adj_close, volume, turnover
FROM dbo.daily_bars
WHERE symbol = ? AND trade_date >= ? AND trade_date <= ?
ORDER BY trade_date;"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (symbol, start_date, end_date))
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [self._bar_from_row(row) for row in rows]

    def has_bar_coverage(self, symbol: str, start_date: date, end_date: date) -> bool:
        """仅当已缓存区间完整包住所需日期时才复用，避免将空日误判为缺 K 线。"""

        sql = """SELECT TOP (1) 1 FROM dbo.daily_bar_coverage
WHERE symbol = ? AND start_date <= ? AND end_date >= ?;"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (symbol, start_date, end_date))
                return cursor.fetchone() is not None
            finally:
                cursor.close()

    def mark_bar_coverage(self, symbol: str, start_date: date, end_date: date) -> None:
        """记录已成功拉取的完整查询范围；日历空档不被当作数据损坏。"""

        sql = """MERGE dbo.daily_bar_coverage WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?)) AS source (symbol, start_date, end_date)
ON target.symbol=source.symbol AND target.start_date=source.start_date AND target.end_date=source.end_date
WHEN MATCHED THEN UPDATE SET fetched_at=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (symbol, start_date, end_date)
VALUES (source.symbol, source.start_date, source.end_date);"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (symbol, start_date, end_date))
            finally:
                cursor.close()

    def upsert_actions(self, symbol: str, actions: list[CorporateAction]) -> None:
        """按来源稳定键保存公司权益字段，不把原始记录序列化进数据库。"""

        if not actions:
            return
        sql = """MERGE dbo.corporate_actions WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?, ?, ?, ?, ?)) AS source
    (symbol, source_key, record_date, ex_date, cash_per_share, bonus_shares_per_share, action_status)
ON target.symbol=source.symbol AND target.source_key=source.source_key
WHEN MATCHED THEN UPDATE SET record_date=source.record_date, ex_date=source.ex_date,
    cash_per_share=source.cash_per_share, bonus_shares_per_share=source.bonus_shares_per_share,
    action_status=source.action_status
WHEN NOT MATCHED THEN INSERT
    (symbol, source_key, record_date, ex_date, cash_per_share, bonus_shares_per_share, action_status)
VALUES (source.symbol, source.source_key, source.record_date, source.ex_date,
    source.cash_per_share, source.bonus_shares_per_share, source.action_status);"""
        # executemany 会按整列推断参数类型；将零值也转成 float，避免 0 与小数混合后被误判为整数列。
        rows = [
            (
                symbol,
                action.source_key or self._action_source_key(action),
                action.record_date,
                action.ex_date,
                float(action.cash_per_share),
                float(action.bonus_shares_per_share),
                action.status,
            )
            for action in actions
        ]
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.executemany(sql, rows)
            finally:
                cursor.close()

    def get_actions(self, symbol: str, start_date: date, end_date: date) -> list[CorporateAction]:
        """读取除权区间相关事件；登记日一并返回以便按持仓日期认定权益。"""

        sql = """SELECT record_date, ex_date, cash_per_share,
    bonus_shares_per_share, action_status, source_key
FROM dbo.corporate_actions
WHERE symbol = ? AND ((ex_date >= ? AND ex_date <= ?) OR ex_date IS NULL)
ORDER BY ex_date;"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (symbol, start_date, end_date))
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [
            CorporateAction(
                symbol=symbol,
                record_date=row[0].date() if hasattr(row[0], "date") else row[0],  # type: ignore[arg-type]
                ex_date=row[1].date() if hasattr(row[1], "date") else row[1],  # type: ignore[arg-type]
                cash_per_share=float(row[2]),
                bonus_shares_per_share=float(row[3]),
                status=str(row[4]),
                source_key=str(row[5]),
            )
            for row in rows
        ]

    def create_backtest(self, run_id: str, params: BacktestParams) -> None:
        """先保存全部可审计输入参数，再由后台 worker 开始运行。"""

        sql = """INSERT INTO dbo.backtest_runs
    (run_id, symbol, start_date, end_date, strategy_code, short_window, long_window,
     ma_period, breakout_period, atr_period, atr_multiplier, momentum_period_days,
     trend_ma_period, max_positions, rebalance_interval_days,
     initial_capital, position_ratio, commission_rate, minimum_commission,
     stamp_duty_rate, slippage_rate, max_holding_days, cooldown_days, target_annual_volatility, status, progress)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0);"""
        values = (
            run_id, params.symbol, params.start_date, params.end_date, params.strategy_code,
            params.short_window, params.long_window, params.ma_period, params.breakout_period,
            params.atr_period, params.atr_multiplier, params.momentum_period_days,
            params.trend_ma_period, params.max_positions, params.rebalance_interval_days,
            params.initial_capital, params.position_ratio,
            params.commission_rate, params.minimum_commission, params.stamp_duty_rate, params.slippage_rate,
            params.max_holding_days, params.cooldown_days, params.target_annual_volatility,
        )
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, values)
                cursor.executemany(
                    "INSERT INTO dbo.backtest_run_symbols (run_id, symbol, ordinal) VALUES (?, ?, ?);",
                    [(run_id, symbol, index) for index, symbol in enumerate(params.symbols or (params.symbol,))],
                )
            finally:
                cursor.close()

    def set_backtest_status(
        self,
        run_id: str,
        status: str,
        progress: int,
        error_message: str | None = None,
    ) -> None:
        """更新状态和进度；错误消息长度受表结构约束并且不保存 Python 堆栈。"""

        sql = """UPDATE dbo.backtest_runs
SET status=?, progress=?, error_message=?, updated_at=SYSUTCDATETIME()
WHERE run_id=?;"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (status, max(0, min(progress, 100)), error_message, run_id))
            finally:
                cursor.close()

    def mark_interrupted_runs(self) -> None:
        """服务重启时关闭遗留任务，避免页面永久显示排队或运行中。"""

        sql = """UPDATE dbo.backtest_runs
SET status='failed', error_message=N'服务重启导致任务中断。', updated_at=SYSUTCDATETIME()
WHERE status IN ('queued', 'running');"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
            finally:
                cursor.close()

    def request_cancel(self, run_id: str) -> bool:
        """只为尚未完成的任务记录取消请求。"""

        sql = """UPDATE dbo.backtest_runs
SET cancel_requested=1, updated_at=SYSUTCDATETIME()
WHERE run_id=? AND status IN ('queued', 'running');"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (run_id,))
                return cursor.rowcount > 0
            finally:
                cursor.close()

    def save_backtest_result(self, run_id: str, result: BacktestResult) -> None:
        """在单一事务中替换摘要、曲线、成交和逐条提示，避免半份回测结果。"""

        metrics = result.metrics
        update_sql = """UPDATE dbo.backtest_runs SET status='completed', progress=100,
    total_return=?, annualized_return=?, max_drawdown=?, annualized_volatility=?,
    sharpe_ratio=?, win_rate=?, closed_trade_count=?, fees_total=?, dividends_total=?,
    benchmark_return=?, error_message=NULL, updated_at=SYSUTCDATETIME()
WHERE run_id=?;"""
        equity_sql = """INSERT INTO dbo.backtest_equity
    (run_id, trade_date, cash, shares, market_value, equity, drawdown, benchmark_equity)
VALUES (?, ?, ?, ?, ?, ?, ?, ?);"""
        trade_sql = """INSERT INTO dbo.backtest_trades
    (run_id, trade_date, signal_date, side, symbol, execution_price, quantity,
     notional, commission, stamp_duty, reason)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"""
        warning_sql = """INSERT INTO dbo.backtest_warnings (run_id, ordinal, message)
VALUES (?, ?, ?);"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    update_sql,
                    (
                        metrics.total_return, metrics.annualized_return, metrics.max_drawdown,
                        metrics.annualized_volatility, metrics.sharpe_ratio, metrics.win_rate,
                        metrics.closed_trade_count, result.fees_total, result.dividends_total,
                        result.benchmark_return, run_id,
                    ),
                )
                cursor.execute("DELETE FROM dbo.backtest_equity WHERE run_id=?;", (run_id,))
                cursor.execute("DELETE FROM dbo.backtest_trades WHERE run_id=?;", (run_id,))
                cursor.execute("DELETE FROM dbo.backtest_warnings WHERE run_id=?;", (run_id,))
                if result.equity_curve:
                    # 按 SQL DECIMAL/BIGINT 的字段语义统一 Python 数值类型，避免批次类型推断歧义。
                    cursor.executemany(
                        equity_sql,
                        [
                            (
                                run_id, item.trade_date, float(item.cash), int(item.shares),
                                float(item.market_value), float(item.equity), float(item.drawdown),
                                float(item.benchmark_equity),
                            )
                            for item in result.equity_curve
                        ],
                    )
                if result.trades:
                    cursor.executemany(
                        trade_sql,
                        [
                            (
                                run_id, item.trade_date, item.signal_date, item.side,
                                item.symbol or result.symbol,
                                float(item.price), int(item.quantity), float(item.notional),
                                float(item.commission), float(item.stamp_duty), item.reason,
                            )
                            for item in result.trades
                        ],
                    )
                if result.warnings:
                    cursor.executemany(
                        warning_sql,
                        [(run_id, index, warning[:500]) for index, warning in enumerate(result.warnings)],
                    )
            finally:
                cursor.close()

    def get_backtest(self, run_id: str) -> dict[str, Any] | None:
        """读取单次回测摘要、日权益、成交和提示。"""

        sql = """SELECT run_id, symbol, start_date, end_date, strategy_code,
    short_window, long_window, ma_period, breakout_period, atr_period, atr_multiplier,
    momentum_period_days, trend_ma_period, max_positions, rebalance_interval_days,
    initial_capital, position_ratio, commission_rate,
    minimum_commission, stamp_duty_rate, slippage_rate, status, progress,
    total_return, annualized_return, max_drawdown, annualized_volatility,
    sharpe_ratio, win_rate, closed_trade_count, fees_total, dividends_total,
    benchmark_return, error_message, created_at, max_holding_days, cooldown_days, target_annual_volatility
FROM dbo.backtest_runs WHERE run_id=?;"""
        names = (
            "run_id", "symbol", "start_date", "end_date", "strategy_code", "short_window",
            "long_window", "ma_period", "breakout_period", "atr_period", "atr_multiplier",
            "momentum_period_days", "trend_ma_period", "max_positions", "rebalance_interval_days",
            "initial_capital", "position_ratio", "commission_rate",
            "minimum_commission", "stamp_duty_rate", "slippage_rate", "status", "progress",
            "total_return", "annualized_return", "max_drawdown", "annualized_volatility",
            "sharpe_ratio", "win_rate", "closed_trade_count", "fees_total", "dividends_total",
            "benchmark_return", "error_message", "created_at",
            "max_holding_days", "cooldown_days", "target_annual_volatility",
        )
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (run_id,))
                row = cursor.fetchone()
                if row is None:
                    return None
                record = dict(zip(names, row))
                cursor.execute(
                    "SELECT symbol FROM dbo.backtest_run_symbols WHERE run_id=? ORDER BY ordinal;",
                    (run_id,),
                )
                symbol_rows = cursor.fetchall()
                if record["status"] in ("queued", "running"):
                    # 页面每秒轮询进度；未完成任务没有账本，省去三次空表查询。
                    curve_rows, trade_rows, warnings = [], [], []
                else:
                    cursor.execute(
                        """SELECT trade_date, cash, shares, market_value, equity, drawdown, benchmark_equity
FROM dbo.backtest_equity WHERE run_id=? ORDER BY trade_date;""",
                        (run_id,),
                    )
                    curve_rows = cursor.fetchall()
                    cursor.execute(
                        """SELECT trade_date, signal_date, side, COALESCE(symbol, ?) AS symbol, execution_price, quantity,
    notional, commission, stamp_duty, reason
FROM dbo.backtest_trades WHERE run_id=? ORDER BY trade_date, trade_id;""",
                        (row[1], run_id),
                    )
                    trade_rows = cursor.fetchall()
                    cursor.execute(
                        "SELECT message FROM dbo.backtest_warnings WHERE run_id=? ORDER BY ordinal;",
                        (run_id,),
                    )
                    warnings = [item[0] for item in cursor.fetchall()]
            finally:
                cursor.close()
        record["run_id"] = str(record["run_id"])
        # 老档案没有关联行，仍按原单股票记录回退展示。
        record["symbols"] = [item[0] for item in symbol_rows] or [record["symbol"]]
        record["equity_curve"] = [
            dict(zip(("trade_date", "cash", "shares", "market_value", "equity", "drawdown", "benchmark_equity"), item))
            for item in curve_rows
        ]
        record["trades"] = [
            dict(zip(("trade_date", "signal_date", "side", "symbol", "price", "quantity", "notional", "commission", "stamp_duty", "reason"), item))
            for item in trade_rows
        ]
        record["warnings"] = warnings
        return record

    def list_backtests(self, offset: int = 0, limit: int = 20) -> tuple[list[dict[str, Any]], int]:
        """分页返回历史回测摘要，limit/offset 由 API 先行校验。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT COUNT_BIG(1) FROM dbo.backtest_runs;")
                total = int(cursor.fetchone()[0])
                cursor.execute(
                    """SELECT run_id, symbol, start_date, end_date, strategy_code,
    COALESCE(NULLIF((SELECT COUNT(*) FROM dbo.backtest_run_symbols AS symbols
                     WHERE symbols.run_id=backtest_runs.run_id), 0), 1) AS symbol_count,
    short_window, long_window, status, progress, total_return, annualized_return,
    max_drawdown, created_at
FROM dbo.backtest_runs ORDER BY created_at DESC
OFFSET ? ROWS FETCH NEXT ? ROWS ONLY;""",
                    (offset, limit),
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()
        names = (
            "run_id", "symbol", "start_date", "end_date", "strategy_code", "symbol_count", "short_window",
            "long_window", "status", "progress", "total_return", "annualized_return",
            "max_drawdown", "created_at",
        )
        items = [dict(zip(names, row)) for row in rows]
        for item in items:
            item["run_id"] = str(item["run_id"])
            # 列表先返回主标的和策略；完整股票池按需打开档案详情查询。
        return items, total

    @staticmethod
    def _action_source_key(action: CorporateAction) -> str:
        """用可读的确定性字段组合去重，不依赖不透明 JSON 序列化。"""

        record = action.record_date.isoformat() if action.record_date else ""
        return f"{record}|{action.ex_date.isoformat()}|{action.cash_per_share:.8f}|{action.bonus_shares_per_share:.8f}"

    @staticmethod
    def _bar_from_row(row: tuple[object, ...]) -> Bar:
        trade_date = row[0].date() if hasattr(row[0], "date") else row[0]
        return Bar(
            trade_date=trade_date,  # type: ignore[arg-type]
            open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]),
            adj_open=float(row[5]), adj_high=float(row[6]), adj_low=float(row[7]),
            adj_close=float(row[8]), volume=float(row[9]), turnover=float(row[10]),
        )
