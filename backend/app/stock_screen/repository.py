"""选股实验的 SQL Server 账本；逐股检查点均以参数化语句写入。"""

from datetime import date
from functools import wraps
import time
from typing import Any

from app.models import Quote
from app.repositories.sql_repository import SqlRepository
from app.stock_screen.rules import rank_candidates


def _fetch_dicts(cursor: Any) -> list[dict[str, Any]]:
    """统一将 DB-API 行转为带字段名的记录，避免位置索引漂移。"""

    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _retry_deadlock_read(function: Any) -> Any:
    """最终排名批量写入时，页面读查询若成为 SQL 死锁牺牲者可短暂重读。"""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(3):
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                message = str(exc).lower()
                if attempt == 2 or not any(token in message for token in ("serialization failure", "deadlock", "死锁", "1205")):
                    raise
                # 仅重试只读请求，不自动重放可能已提交的写事务。
                time.sleep(0.1 * (attempt + 1))
        raise AssertionError("不可达的 SQL 重试路径")

    return wrapped


class StockScreenRepository:
    def __init__(self, repository: SqlRepository) -> None:
        self._repository = repository

    def create_run(self, run_id: str, cutoff: date, candidate_count: int, quotes: list[Quote]) -> None:
        """任务和固定股票池同事务落库；失败不留下半个扫描。"""

        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("""INSERT INTO dbo.stock_screen_runs
                    (run_id, status, signal_cutoff, requested_count, candidate_count, rule_version)
                    VALUES (?, 'queued', ?, ?, ?, 'v1');""", (run_id, cutoff, len(quotes), candidate_count))
                cursor.executemany("""INSERT INTO dbo.stock_screen_items
                    (run_id, symbol, name, snapshot_turnover, status) VALUES (?, ?, ?, ?, 'pending');""",
                    [(run_id, q.symbol, q.name[:100], float(q.turnover)) for q in quotes])
            finally:
                cursor.close()

    @_retry_deadlock_read
    def list_runs(self) -> list[dict[str, Any]]:
        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("""SELECT TOP (30) r.run_id, r.status, r.signal_cutoff, r.requested_count,
                    r.candidate_count, r.rule_version, r.created_at, r.completed_at, r.error_message,
                    SUM(CASE WHEN i.status <> 'pending' THEN 1 ELSE 0 END) AS processed_count,
                    SUM(CASE WHEN i.status = 'eligible' THEN 1 ELSE 0 END) AS eligible_count
                    FROM dbo.stock_screen_runs r JOIN dbo.stock_screen_items i ON i.run_id = r.run_id
                    GROUP BY r.run_id, r.status, r.signal_cutoff, r.requested_count, r.candidate_count,
                        r.rule_version, r.created_at, r.completed_at, r.error_message
                    ORDER BY r.created_at DESC;""")
                return _fetch_dicts(cursor)
            finally:
                cursor.close()

    @_retry_deadlock_read
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """一次读取详情所需的结构化股票行和观察行。"""

        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("""SELECT run_id, status, signal_cutoff, requested_count, candidate_count,
                    rule_version, created_at, completed_at, error_message
                    FROM dbo.stock_screen_runs WHERE run_id=?;""", (run_id,))
                runs = _fetch_dicts(cursor)
                if not runs:
                    return None
                cursor.execute("""SELECT symbol, name, snapshot_turnover, status, signal_date,
                    signal_close, momentum_score, recent_5_return, reversal_score,
                    momentum_rank, reversal_rank, error_message
                    FROM dbo.stock_screen_items WHERE run_id=? ORDER BY symbol;""", (run_id,))
                items = _fetch_dicts(cursor)
                cursor.execute("""SELECT o.symbol, o.horizon_days, o.outcome_date, o.outcome_close,
                    o.return_rate FROM dbo.stock_screen_outcomes o WHERE o.run_id=?;""", (run_id,))
                outcomes = _fetch_dicts(cursor)
                return {**runs[0], "items": items, "outcomes": outcomes}
            finally:
                cursor.close()

    def next_active_run(self) -> dict[str, Any] | None:
        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("""SELECT TOP (1) run_id, signal_cutoff, candidate_count
                    FROM dbo.stock_screen_runs WHERE status IN ('queued', 'running') ORDER BY created_at;""")
                rows = _fetch_dicts(cursor)
                return rows[0] if rows else None
            finally:
                cursor.close()

    def set_running(self, run_id: str) -> None:
        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("UPDATE dbo.stock_screen_runs SET status='running', error_message=NULL WHERE run_id=? AND status='queued';", (run_id,))
            finally:
                cursor.close()

    def next_pending_item(self, run_id: str) -> dict[str, Any] | None:
        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("""SELECT TOP (1) i.symbol, i.name
                    FROM dbo.stock_screen_items i JOIN dbo.stock_screen_runs r ON r.run_id=i.run_id
                    WHERE i.run_id=? AND i.status='pending' AND r.status='running' ORDER BY i.symbol;""", (run_id,))
                rows = _fetch_dicts(cursor)
                return rows[0] if rows else None
            finally:
                cursor.close()

    def save_signal(self, run_id: str, symbol: str, score: tuple[float | None, float | None, date, float] | None) -> None:
        """每只股票独立提交：中断时仅当前未完成股票需要重做。"""

        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                if score is None:
                    cursor.execute("""UPDATE dbo.stock_screen_items SET status='excluded', error_message=N'有效日线不足 261 根或价格无效'
                        WHERE run_id=? AND symbol=? AND status='pending';""", (run_id, symbol))
                else:
                    momentum, recent5, signal_date, close = score
                    # 历史覆盖充分即进入等权基准；趋势过滤只决定是否获得某条规则的分数。
                    status = 'eligible'
                    cursor.execute("""UPDATE dbo.stock_screen_items SET status=?, signal_date=?, signal_close=?,
                        momentum_score=?, recent_5_return=?, error_message=NULL
                        WHERE run_id=? AND symbol=? AND status='pending';""",
                        (status, signal_date, close, momentum, recent5, run_id, symbol))
            finally:
                cursor.close()

    def set_source_error(self, run_id: str, message: str) -> None:
        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("""UPDATE dbo.stock_screen_runs SET error_message=?
                    WHERE run_id=? AND status='running';""", (message[:500], run_id))
            finally:
                cursor.close()

    def finalize_run(self, run_id: str, candidate_count: int) -> None:
        """等所有股票都有检查点后，按同一交易日和同一批样本统一计算相对分数。"""

        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT status FROM dbo.stock_screen_runs WITH (UPDLOCK) WHERE run_id=?;", (run_id,))
                current = cursor.fetchone()
                if not current or current[0] != 'running':
                    return
                cursor.execute("SELECT COUNT(*) FROM dbo.stock_screen_items WHERE run_id=? AND status='pending';", (run_id,))
                if cursor.fetchone()[0]:
                    return
                # 兼容修正口径前已处理的检查点：有完整信号日但未过趋势的股票仍属于数据合格基准。
                cursor.execute("""UPDATE dbo.stock_screen_items SET status='eligible'
                    WHERE run_id=? AND status='excluded' AND signal_date IS NOT NULL
                      AND momentum_score IS NULL AND recent_5_return IS NULL AND error_message IS NULL;""", (run_id,))
                cursor.execute("""SELECT symbol, signal_date, momentum_score, recent_5_return
                    FROM dbo.stock_screen_items WHERE run_id=? AND status='eligible';""", (run_id,))
                rows = _fetch_dicts(cursor)
                if not rows:
                    cursor.execute("""UPDATE dbo.stock_screen_runs SET status='failed', completed_at=SYSUTCDATETIME(),
                        error_message=N'没有股票满足数据覆盖与趋势条件' WHERE run_id=?;""", (run_id,))
                    return
                latest = max(row['signal_date'] for row in rows)
                current_rows = [row for row in rows if row['signal_date'] == latest]
                recent = [float(row['recent_5_return']) for row in current_rows if row['recent_5_return'] is not None]
                mean_recent = sum(recent) / len(recent) if recent else 0.0
                for row in current_rows:
                    row['reversal_score'] = (mean_recent - float(row['recent_5_return'])) if row['recent_5_return'] is not None else None
                ranks = rank_candidates(current_rows, candidate_count)
                for row in rows:
                    if row['signal_date'] != latest:
                        cursor.execute("""UPDATE dbo.stock_screen_items SET status='excluded',
                            error_message=N'最近日线落后于本批信号日' WHERE run_id=? AND symbol=?;""", (run_id, row['symbol']))
                        continue
                    momentum_rank, reversal_rank = ranks[row['symbol']]
                    cursor.execute("""UPDATE dbo.stock_screen_items SET reversal_score=?, momentum_rank=?, reversal_rank=?
                        WHERE run_id=? AND symbol=?;""",
                        (row['reversal_score'], momentum_rank, reversal_rank, run_id, row['symbol']))
                cursor.execute("""UPDATE dbo.stock_screen_runs SET status='completed', completed_at=SYSUTCDATETIME(),
                    error_message=NULL WHERE run_id=?;""", (run_id,))
            finally:
                cursor.close()

    def cancel_run(self, run_id: str) -> bool:
        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("""UPDATE dbo.stock_screen_runs SET status='cancelled', completed_at=SYSUTCDATETIME()
                    WHERE run_id=? AND status IN ('queued', 'running');""", (run_id,))
                return cursor.rowcount > 0
            finally:
                cursor.close()

    def due_evaluations(self, today: date, limit: int = 20) -> list[dict[str, Any]]:
        """同一只股票每日最多尝试一次，防止停牌或假期反复打上游接口。"""

        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("""SELECT TOP (?) i.run_id, i.symbol, i.signal_date, i.signal_close
                    FROM dbo.stock_screen_items i JOIN dbo.stock_screen_runs r ON r.run_id=i.run_id
                    WHERE r.status='completed' AND i.status='eligible'
                      AND (i.last_evaluated_date IS NULL OR i.last_evaluated_date < ?)
                      AND ((i.signal_date <= DATEADD(day, -7, ?)
                            AND NOT EXISTS (SELECT 1 FROM dbo.stock_screen_outcomes o
                                            WHERE o.run_id=i.run_id AND o.symbol=i.symbol AND o.horizon_days=5))
                           OR (i.signal_date <= DATEADD(day, -28, ?)
                            AND NOT EXISTS (SELECT 1 FROM dbo.stock_screen_outcomes o
                                            WHERE o.run_id=i.run_id AND o.symbol=i.symbol AND o.horizon_days=20)))
                    ORDER BY i.signal_date, i.run_id, i.symbol;""", (limit, today, today, today))
                return _fetch_dicts(cursor)
            finally:
                cursor.close()

    def save_evaluation(self, run_id: str, symbol: str, today: date,
                        outcomes: list[tuple[int, date, float, float]]) -> None:
        with self._repository._transaction() as connection:
            cursor = connection.cursor()
            try:
                for horizon, outcome_date, close, return_rate in outcomes:
                    cursor.execute("""IF NOT EXISTS (SELECT 1 FROM dbo.stock_screen_outcomes
                        WHERE run_id=? AND symbol=? AND horizon_days=?)
                        INSERT INTO dbo.stock_screen_outcomes
                        (run_id, symbol, horizon_days, outcome_date, outcome_close, return_rate)
                        VALUES (?, ?, ?, ?, ?, ?);""",
                        (run_id, symbol, horizon, run_id, symbol, horizon, outcome_date, close, return_rate))
                cursor.execute("""UPDATE dbo.stock_screen_items SET last_evaluated_date=?
                    WHERE run_id=? AND symbol=?;""", (today, run_id, symbol))
            finally:
                cursor.close()
