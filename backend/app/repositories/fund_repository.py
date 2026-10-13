"""基金行情与基金模拟账本的数据访问层。"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import DatabaseSettings
from app.database import create_connection
from app.fund_data.models import FundBar, FundDistribution, FundInstrument, FundNavPoint

logger = logging.getLogger(__name__)
_CHINA_TZ = timezone(timedelta(hours=8))


class FundRepository:
    """所有基金读写使用独立 SQL Server 事务和绑定参数。"""

    def __init__(self, settings: DatabaseSettings, connection_factory: Callable[[], Any] | None = None) -> None:
        self._connection_factory = connection_factory or (lambda: create_connection(settings))

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self._connection_factory()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception("基金 SQL Server 事务失败")
            raise
        finally:
            connection.close()

    @staticmethod
    def _db_datetime(value: datetime | None) -> datetime | None:
        """DATETIME2 不保存时区；将带时区时间转换为北京时间墙上值。"""

        if value is None:
            return None
        return value.astimezone(_CHINA_TZ).replace(tzinfo=None) if value.tzinfo else value

    def upsert_instruments(self, instruments: list[FundInstrument]) -> None:
        sql = """MERGE dbo.fund_instruments WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?, ?, ?, ?, ?)) AS source (fund_type, symbol, name, subscription_status, redemption_status, fee_description, category)
ON target.fund_type=source.fund_type AND target.symbol=source.symbol
WHEN MATCHED THEN UPDATE SET name=source.name, subscription_status=source.subscription_status,
    redemption_status=source.redemption_status, fee_description=source.fee_description, category=source.category, updated_at=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (fund_type, symbol, name, subscription_status, redemption_status, fee_description, category)
VALUES (source.fund_type, source.symbol, source.name, source.subscription_status, source.redemption_status, source.fee_description, source.category);"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                for item in instruments:
                    cursor.execute(sql, (
                        item.fund_type, item.symbol, item.name, item.subscription_status,
                        item.redemption_status, item.fee_description,
                        item.category,
                    ))
            finally:
                cursor.close()

    def search_funds(self, query: str, fund_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = """SELECT TOP (?) fund_type, symbol, name, subscription_status, redemption_status, fee_description, category
FROM dbo.fund_instruments WHERE (symbol LIKE ? OR name LIKE ?)"""
        values: list[Any] = [max(1, min(limit, 200)), f"%{query.strip()}%", f"%{query.strip()}%"]
        if fund_type:
            sql += " AND fund_type=?"
            values.append(fund_type)
        sql += " ORDER BY name, symbol;"
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, tuple(values))
                names = ("fund_type", "symbol", "name", "subscription_status", "redemption_status", "fee_description", "category")
                return [dict(zip(names, row)) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def upsert_bars(self, fund_type: str, symbol: str, bars: list[FundBar]) -> None:
        sql = """MERGE dbo.fund_daily_bars WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)) AS source
    (fund_type, symbol, trade_date, open_price, high_price, low_price, close_price, volume, turnover)
ON target.fund_type=source.fund_type AND target.symbol=source.symbol AND target.trade_date=source.trade_date
WHEN MATCHED THEN UPDATE SET open_price=source.open_price, high_price=source.high_price,
    low_price=source.low_price, close_price=source.close_price, volume=source.volume, turnover=source.turnover
WHEN NOT MATCHED THEN INSERT (fund_type, symbol, trade_date, open_price, high_price, low_price, close_price, volume, turnover)
VALUES (source.fund_type, source.symbol, source.trade_date, source.open_price, source.high_price,
    source.low_price, source.close_price, source.volume, source.turnover);"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                for bar in bars:
                    cursor.execute(sql, (fund_type, symbol, bar.trade_date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.turnover))
            finally:
                cursor.close()

    def get_bars(self, fund_type: str, symbol: str, start_date: date, end_date: date) -> list[FundBar]:
        sql = """SELECT trade_date, open_price, high_price, low_price, close_price, volume, turnover
FROM dbo.fund_daily_bars WHERE fund_type=? AND symbol=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date;"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (fund_type, symbol, start_date, end_date))
                return [FundBar(*row) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def upsert_nav_history(self, fund_type: str, symbol: str, points: list[FundNavPoint]) -> None:
        sql = """MERGE dbo.fund_nav_history WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?, ?, ?, ?)) AS source (fund_type, symbol, trade_date, unit_nav, accumulated_nav, daily_return)
ON target.fund_type=source.fund_type AND target.symbol=source.symbol AND target.trade_date=source.trade_date
WHEN MATCHED THEN UPDATE SET unit_nav=source.unit_nav, accumulated_nav=source.accumulated_nav, daily_return=source.daily_return
WHEN NOT MATCHED THEN INSERT (fund_type, symbol, trade_date, unit_nav, accumulated_nav, daily_return)
VALUES (source.fund_type, source.symbol, source.trade_date, source.unit_nav, source.accumulated_nav, source.daily_return);"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                for point in points:
                    cursor.execute(sql, (fund_type, symbol, point.trade_date, point.unit_nav, point.accumulated_nav, point.daily_return))
            finally:
                cursor.close()

    def get_nav_history(self, fund_type: str, symbol: str, start_date: date | None = None, end_date: date | None = None) -> list[FundNavPoint]:
        sql = """SELECT trade_date, unit_nav, accumulated_nav, daily_return FROM dbo.fund_nav_history
WHERE fund_type=? AND symbol=? AND (? IS NULL OR trade_date>=?) AND (? IS NULL OR trade_date<=?) ORDER BY trade_date;"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (fund_type, symbol, start_date, start_date, end_date, end_date))
                return [FundNavPoint(*row) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def upsert_distributions(self, fund_type: str, symbol: str, items: list[FundDistribution]) -> None:
        sql = """MERGE dbo.fund_distributions WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?, ?, ?)) AS source (fund_type, symbol, ex_date, cash_per_unit, source_key)
ON target.fund_type=source.fund_type AND target.symbol=source.symbol AND target.ex_date=source.ex_date AND target.source_key=source.source_key
WHEN MATCHED THEN UPDATE SET cash_per_unit=source.cash_per_unit
WHEN NOT MATCHED THEN INSERT (fund_type, symbol, ex_date, cash_per_unit, source_key)
VALUES (source.fund_type, source.symbol, source.ex_date, source.cash_per_unit, source.source_key);"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                for item in items:
                    cursor.execute(sql, (fund_type, symbol, item.ex_date, item.cash_per_unit, item.source_key))
            finally:
                cursor.close()

    def get_distributions(self, fund_type: str, symbol: str) -> list[FundDistribution]:
        """读取已保存的分红事件，供净值型账户逐份派现。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("""SELECT ex_date, cash_per_unit, source_key FROM dbo.fund_distributions
WHERE fund_type=? AND symbol=? ORDER BY ex_date, source_key;""", (fund_type, symbol))
                return [FundDistribution(*row) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def create_session(self, session_id: str, payload: dict[str, Any]) -> None:
        """创建独立基金账户、持仓范围和初始资金曲线。"""

        fields = (
            "session_id", "task_name", "fund_type", "strategy_code", "status", "started_at", "initial_capital", "cash",
            "market_value", "equity", "total_return", "max_drawdown", "peak_equity", "commission_rate", "minimum_commission",
            "stamp_duty_rate", "slippage_rate", "subscription_fee_rate", "redemption_fee_rate", "ma_period", "breakout_period",
            "atr_period", "atr_multiplier", "short_ma_period", "long_ma_period", "momentum_period_days", "trend_ma_period",
            "max_positions", "rebalance_interval_days", "risk_lookback_days", "last_rebalanced_at",
        )
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        params = dict(payload)
        params.update(session_id=session_id, status="running")
        params.setdefault("market_value", 0)
        params.setdefault("equity", params["initial_capital"])
        params.setdefault("cash", params["initial_capital"])
        params.setdefault("total_return", 0)
        params.setdefault("max_drawdown", 0)
        params.setdefault("peak_equity", params["initial_capital"])
        # 兼容旧任务记录，未显式传入该列时沿用低波动策略的默认观察期。
        params.setdefault("risk_lookback_days", 60)
        params.setdefault("last_rebalanced_at", None)
        values = [self._db_datetime(params[name]) if name in {"started_at", "last_rebalanced_at"} else params[name] for name in fields]
        insert_session = f"INSERT dbo.fund_paper_sessions ({columns}) VALUES ({marks});"
        insert_fund = """INSERT dbo.fund_paper_session_funds (session_id, fund_type, symbol, name)
VALUES (?, ?, ?, ?);"""
        insert_equity = """INSERT dbo.fund_paper_equity (session_id, trade_date, cash, market_value, equity, drawdown)
VALUES (?, ?, ?, ?, ?, ?);"""
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(insert_session, tuple(values))
                for fund in params["funds"]:
                    cursor.execute(insert_fund, (session_id, params["fund_type"], fund["symbol"], fund["name"]))
                started = params["started_at"]
                cursor.execute(insert_equity, (session_id, started.date(), params["cash"], 0, params["cash"], 0))
            finally:
                cursor.close()

    def list_sessions(self) -> list[dict[str, Any]]:
        sql = """SELECT session_id, task_name, fund_type, strategy_code, status, started_at, completed_at,
initial_capital, cash, market_value, equity, total_return, max_drawdown, error_message
FROM dbo.fund_paper_sessions ORDER BY started_at DESC;"""
        names = ("session_id", "task_name", "fund_type", "strategy_code", "status", "started_at", "completed_at",
                 "initial_capital", "cash", "market_value", "equity", "total_return", "max_drawdown", "error_message")
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                items = [dict(zip(names, row)) for row in cursor.fetchall()]
                for item in items:
                    item["session_id"] = str(item["session_id"])
                return items
            finally:
                cursor.close()

    def get_running_sessions(self) -> list[dict[str, Any]]:
        sql = """SELECT session_id, task_name, fund_type, strategy_code, started_at, initial_capital, cash, market_value, equity,
total_return, max_drawdown, peak_equity,
commission_rate, minimum_commission, stamp_duty_rate, slippage_rate, subscription_fee_rate, redemption_fee_rate,
ma_period, breakout_period, atr_period, atr_multiplier, short_ma_period, long_ma_period, momentum_period_days,
trend_ma_period, max_positions, rebalance_interval_days, risk_lookback_days, last_rebalanced_at
FROM dbo.fund_paper_sessions WHERE status='running';"""
        names = ("session_id", "task_name", "fund_type", "strategy_code", "started_at", "initial_capital", "cash",
                 "market_value", "equity", "total_return", "max_drawdown", "peak_equity",
                 "commission_rate", "minimum_commission", "stamp_duty_rate", "slippage_rate", "subscription_fee_rate",
                 "redemption_fee_rate", "ma_period", "breakout_period", "atr_period", "atr_multiplier", "short_ma_period",
                  "long_ma_period", "momentum_period_days", "trend_ma_period", "max_positions", "rebalance_interval_days",
                  "risk_lookback_days", "last_rebalanced_at")
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                sessions = [dict(zip(names, row)) for row in cursor.fetchall()]
                for session in sessions:
                    cursor.execute("""SELECT symbol, name, shares, entry_date, entry_price, highest_price, entry_atr, cost_basis, last_price, last_quote_at
FROM dbo.fund_paper_session_funds WHERE session_id=? ORDER BY symbol;""", (session["session_id"],))
                    keys = ("symbol", "name", "shares", "entry_date", "entry_price", "highest_price", "entry_atr", "cost_basis", "last_price", "last_quote_at")
                    session["funds"] = [dict(zip(keys, row)) for row in cursor.fetchall()]
                    cursor.execute("""SELECT idempotency_key, fund_type, symbol, side, order_mode, status, signal_date,
execution_date, amount, quantity, execution_price, fee, reason FROM dbo.fund_paper_orders WHERE session_id=? ORDER BY order_id;""", (session["session_id"],))
                    keys = ("idempotency_key", "fund_type", "symbol", "side", "order_mode", "status", "signal_date", "execution_date", "amount", "quantity", "execution_price", "fee", "reason")
                    session["orders"] = [dict(zip(keys, row)) for row in cursor.fetchall()]
                return sessions
            finally:
                cursor.close()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        sql = """SELECT session_id, task_name, fund_type, strategy_code, status, started_at, completed_at,
initial_capital, cash, market_value, equity, total_return, max_drawdown, commission_rate, minimum_commission,
stamp_duty_rate, slippage_rate, subscription_fee_rate, redemption_fee_rate, ma_period, breakout_period,
atr_period, atr_multiplier, short_ma_period, long_ma_period, momentum_period_days, trend_ma_period,
max_positions, rebalance_interval_days, risk_lookback_days, last_rebalanced_at, error_message
FROM dbo.fund_paper_sessions WHERE session_id=?;"""
        names = ("session_id", "task_name", "fund_type", "strategy_code", "status", "started_at", "completed_at",
                 "initial_capital", "cash", "market_value", "equity", "total_return", "max_drawdown", "commission_rate",
                 "minimum_commission", "stamp_duty_rate", "slippage_rate", "subscription_fee_rate", "redemption_fee_rate",
                 "ma_period", "breakout_period", "atr_period", "atr_multiplier", "short_ma_period", "long_ma_period",
                  "momentum_period_days", "trend_ma_period", "max_positions", "rebalance_interval_days",
                  "risk_lookback_days", "last_rebalanced_at", "error_message")
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (session_id,))
                row = cursor.fetchone()
                if row is None:
                    return None
                result = dict(zip(names, row))
                result["session_id"] = str(result["session_id"])
                for field, query, keys in (
                    ("funds", "SELECT symbol, name, shares, entry_date, entry_price, highest_price, entry_atr, cost_basis, last_price, last_quote_at FROM dbo.fund_paper_session_funds WHERE session_id=? ORDER BY symbol;", ("symbol", "name", "shares", "entry_date", "entry_price", "highest_price", "entry_atr", "cost_basis", "last_price", "last_quote_at")),
                    ("equity_curve", "SELECT trade_date, cash, market_value, equity, drawdown FROM dbo.fund_paper_equity WHERE session_id=? ORDER BY trade_date;", ("trade_date", "cash", "market_value", "equity", "drawdown")),
                    ("orders", "SELECT order_id, idempotency_key, fund_type, symbol, side, order_mode, status, signal_date, execution_date, amount, quantity, execution_price, fee, reason FROM dbo.fund_paper_orders WHERE session_id=? ORDER BY order_id DESC;", ("order_id", "idempotency_key", "fund_type", "symbol", "side", "order_mode", "status", "signal_date", "execution_date", "amount", "quantity", "execution_price", "fee", "reason")),
                    ("trades", "SELECT trade_id, fund_type, symbol, trade_date, signal_date, side, execution_price, quantity, notional, fee, reason FROM dbo.fund_paper_trades WHERE session_id=? ORDER BY trade_date DESC, trade_id DESC;", ("trade_id", "fund_type", "symbol", "trade_date", "signal_date", "side", "execution_price", "quantity", "notional", "fee", "reason")),
                ):
                    cursor.execute(query, (session_id,))
                    result[field] = [dict(zip(keys, item)) for item in cursor.fetchall()]
                return result
            finally:
                cursor.close()

    def apply_fund_update(
        self,
        session_id: str,
        processed_at: datetime,
        account: dict[str, float],
        fund_updates: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        equity_point: dict[str, Any],
    ) -> bool:
        """锁定任务并在同一事务提交余额、持仓、订单、成交与账户曲线。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT status FROM dbo.fund_paper_sessions WITH (UPDLOCK, HOLDLOCK) WHERE session_id=?;", (session_id,))
                session = cursor.fetchone()
                if session is None or session[0] != "running":
                    return False
                for item in fund_updates:
                    cursor.execute("""UPDATE dbo.fund_paper_session_funds SET shares=?, entry_date=?, entry_price=?, highest_price=?, entry_atr=?,
cost_basis=?, last_price=?, last_quote_at=? WHERE session_id=? AND fund_type=? AND symbol=?;""", (
                        item["shares"], item.get("entry_date"), item.get("entry_price"), item.get("highest_price"), item.get("entry_atr"), item["cost_basis"], item.get("last_price"),
                        self._db_datetime(item.get("last_quote_at")), session_id, item["fund_type"], item["symbol"],
                    ))
                for order in orders:
                    cursor.execute("""MERGE dbo.fund_paper_orders WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)) AS source
    (session_id, fund_type, symbol, side, order_mode, status, idempotency_key, signal_date, execution_date, amount, quantity, execution_price, fee)
ON target.idempotency_key=source.idempotency_key
WHEN MATCHED THEN UPDATE SET status=source.status, execution_date=source.execution_date,
    amount=source.amount, quantity=source.quantity, execution_price=source.execution_price, fee=source.fee
WHEN NOT MATCHED THEN INSERT (session_id, fund_type, symbol, side, order_mode, status, idempotency_key,
    signal_date, execution_date, amount, quantity, execution_price, fee, reason)
VALUES (source.session_id, source.fund_type, source.symbol, source.side, source.order_mode, source.status,
    source.idempotency_key, source.signal_date, source.execution_date, source.amount, source.quantity,
    source.execution_price, source.fee, ?);""", (
                        session_id, order["fund_type"], order["symbol"], order["side"], order["order_mode"], order["status"],
                        order["idempotency_key"], order["signal_date"], order.get("execution_date"), order.get("amount", 0),
                        order.get("quantity", 0), order.get("execution_price"), order.get("fee", 0), order.get("reason", "策略信号"),
                    ))
                for trade in trades:
                    cursor.execute("""INSERT dbo.fund_paper_trades
(order_id, session_id, fund_type, symbol, trade_date, signal_date, side, execution_price, quantity, notional, fee, reason)
SELECT order_id, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? FROM dbo.fund_paper_orders
WHERE idempotency_key=? AND status='filled' AND NOT EXISTS
(SELECT 1 FROM dbo.fund_paper_trades WHERE order_id= dbo.fund_paper_orders.order_id);""", (
                        session_id, trade["fund_type"], trade["symbol"], trade["trade_date"], trade["signal_date"], trade["side"],
                        trade["execution_price"], trade["quantity"], trade["notional"], trade["fee"], trade["reason"], trade["idempotency_key"],
                    ))
                cursor.execute("""UPDATE dbo.fund_paper_sessions SET cash=?, market_value=?, equity=?, total_return=?,
max_drawdown=?, peak_equity=?, last_processed_at=?, last_rebalanced_at=?, error_message=NULL WHERE session_id=? AND status='running';""", (
                    account["cash"], account["market_value"], account["equity"], account["total_return"], account["max_drawdown"],
                    account["peak_equity"], self._db_datetime(processed_at),
                    self._db_datetime(account.get("last_rebalanced_at")), session_id,
                ))
                cursor.execute("""MERGE dbo.fund_paper_equity WITH (HOLDLOCK) AS target
USING (VALUES (?, ?, ?, ?, ?, ?)) AS source (session_id, trade_date, cash, market_value, equity, drawdown)
ON target.session_id=source.session_id AND target.trade_date=source.trade_date
WHEN MATCHED THEN UPDATE SET cash=source.cash, market_value=source.market_value, equity=source.equity, drawdown=source.drawdown
WHEN NOT MATCHED THEN INSERT (session_id, trade_date, cash, market_value, equity, drawdown)
VALUES (source.session_id, source.trade_date, source.cash, source.market_value, source.equity, source.drawdown);""", (
                    session_id, equity_point["trade_date"], equity_point["cash"], equity_point["market_value"],
                    equity_point["equity"], equity_point["drawdown"],
                ))
                return True
            finally:
                cursor.close()

    def cancel_stale_orders_on_start(self) -> int:
        """重启时取消停机期间可能错过执行净值的开放式基金订单。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("UPDATE dbo.fund_paper_orders SET status='cancelled' WHERE order_mode='nav_next' AND status='pending';")
                return max(0, cursor.rowcount)
            finally:
                cursor.close()

    def stop_session(self, session_id: str, stopped_at: datetime) -> bool:
        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("UPDATE dbo.fund_paper_sessions SET status='stopped', completed_at=? WHERE session_id=? AND status='running';", (self._db_datetime(stopped_at), session_id))
                if cursor.rowcount <= 0:
                    return False
                cursor.execute("UPDATE dbo.fund_paper_orders SET status='cancelled' WHERE session_id=? AND status='pending';", (session_id,))
                return True
            finally:
                cursor.close()

    def delete_session(self, session_id: str) -> bool:
        """只删除已停止任务，并按外键依赖顺序在同一事务清除完整账本。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                # 锁定任务行，避免 worker 在检查状态后同时写入订单、持仓或曲线。
                cursor.execute("SELECT status FROM dbo.fund_paper_sessions WITH (UPDLOCK, HOLDLOCK) WHERE session_id=?;", (session_id,))
                row = cursor.fetchone()
                if row is None or row[0] != "stopped":
                    return False
                # 成交依赖订单，订单依赖任务持仓，因此按子表到主表的顺序删除。
                for table in ("fund_paper_trades", "fund_paper_orders", "fund_paper_equity", "fund_paper_session_funds"):
                    cursor.execute(f"DELETE FROM dbo.{table} WHERE session_id=?;", (session_id,))
                cursor.execute("DELETE FROM dbo.fund_paper_sessions WHERE session_id=? AND status='stopped';", (session_id,))
                return cursor.rowcount == 1
            finally:
                cursor.close()

    def set_session_error(self, session_id: str, message: str) -> None:
        """记录单任务 worker 错误，不影响其他基金或股票实验。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("UPDATE dbo.fund_paper_sessions SET status='failed', error_message=?, completed_at=SYSUTCDATETIME() WHERE session_id=? AND status='running';", (message[:500], session_id))
            finally:
                cursor.close()

    def set_session_message(self, session_id: str, message: str) -> None:
        """记录可恢复的数据源提示，不终止独立实验任务。"""

        with self._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("UPDATE dbo.fund_paper_sessions SET error_message=? WHERE session_id=? AND status='running';", (message[:500], session_id))
            finally:
                cursor.close()
