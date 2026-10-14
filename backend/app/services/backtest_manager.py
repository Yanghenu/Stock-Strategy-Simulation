"""单 worker 回测任务管理；取消通过协作式事件在行情/逐日计算边界生效。"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.backtest.engine import run_backtest, validate_backtest_params
from app.backtest.rotation import run_rotation_backtest
from app.market_data.akshare_source import MarketDataSourceError
from app.market_data.service import MarketDataServiceError
from app.models import BacktestParams

logger = logging.getLogger(__name__)


class BacktestManager:
    """后台顺序执行回测并将状态、进度和完整结果写入 SQL Server。"""

    def __init__(self, repository, market_data) -> None:
        self._repository = repository
        self._market_data = market_data
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest")
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._closed = False

    def recover_interrupted_runs(self) -> None:
        """服务重启后将遗留的排队/运行任务标记为中断，不恢复半成品。"""

        self._repository.mark_interrupted_runs()

    def submit(self, params: BacktestParams) -> str:
        """先校验和保存任务，再排入单 worker 队列。"""

        validate_backtest_params(params)
        with self._lock:
            if self._closed:
                raise RuntimeError("回测服务正在关闭")
        # 多股回测逐只满足外键；纸面任务与本队列完全隔离。
        for symbol in params.symbols or (params.symbol,):
            self._market_data.ensure_stock(symbol)
        run_id = str(uuid4())
        self._repository.create_backtest(run_id, params)
        cancel_event = threading.Event()
        with self._lock:
            self._events[run_id] = cancel_event
            self._executor.submit(self._run, run_id, params, cancel_event)
        return run_id

    def cancel(self, run_id: str) -> bool:
        """请求取消未完成任务；已完成/不存在的任务返回 False。"""

        with self._lock:
            event = self._events.get(run_id)
            if event is None or not self._repository.request_cancel(run_id):
                return False
            event.set()
            return True

    def _run(self, run_id: str, params: BacktestParams, cancel_event: threading.Event) -> None:
        try:
            self._repository.set_backtest_status(run_id, "running", 0)
            if cancel_event.is_set():
                raise InterruptedError("回测已取消")
            symbols = params.symbols or (params.symbol,)
            if params.strategy_code == "momentum_rotation":
                history_window = max(params.trend_ma_period, params.momentum_period_days + 1)
            elif params.strategy_code == "breakout_ma_atr":
                history_window = max(params.ma_period, params.breakout_period, params.atr_period + 1)
            elif params.strategy_code == "defensive_mean_reversion":
                # 日历天预热窗口沿用行情层换算约定，覆盖趋势斜率、波动率及 ATR 所需交易日。
                history_window = max(params.trend_ma_period + 20, 21, params.atr_period + 1)
            else:
                history_window = params.long_window
            bars_by_symbol = {}
            actions_by_symbol = {}
            action_warnings: list[str] = []
            for index, symbol in enumerate(symbols, start=1):
                if cancel_event.is_set():
                    raise InterruptedError("回测已取消")
                bars, actions, warnings = self._market_data.get_backtest_inputs(
                    symbol, params.start_date, params.end_date, history_window,
                )
                bars_by_symbol[symbol] = bars
                actions_by_symbol[symbol] = actions
                action_warnings.extend(warnings)
                # 多标的历史加载会花时间；及时展示进度且不增加额外行情请求。
                self._repository.set_backtest_status(run_id, "running", int(index * 15 / len(symbols)))

            last_reported = -5

            def report_progress(completed: int, total: int) -> None:
                nonlocal last_reported
                percentage = 15 + int(completed * 85 / max(total, 1))
                if percentage == 100 or percentage >= last_reported + 5:
                    self._repository.set_backtest_status(run_id, "running", percentage)
                    last_reported = percentage

            if params.strategy_code == "momentum_rotation":
                result = run_rotation_backtest(
                    bars_by_symbol, actions_by_symbol, params,
                    cancel_check=cancel_event.is_set, progress_callback=report_progress,
                )
            else:
                result = run_backtest(
                    bars_by_symbol[params.symbol], actions_by_symbol[params.symbol], params,
                    cancel_check=cancel_event.is_set, progress_callback=report_progress,
                )
            if cancel_event.is_set():
                raise InterruptedError("回测已取消")
            result.warnings.extend(action_warnings)
            self._repository.save_backtest_result(run_id, result)
        except InterruptedError:
            self._repository.set_backtest_status(run_id, "cancelled", 0)
        except (MarketDataSourceError, MarketDataServiceError) as exc:
            logger.exception("回测行情加载失败，run_id=%s", run_id)
            self._repository.set_backtest_status(run_id, "failed", 0, str(exc)[:500])
        except Exception:
            logger.exception("回测执行失败，run_id=%s", run_id)
            self._repository.set_backtest_status(run_id, "failed", 0, "回测执行失败，请查看后端日志。")
        finally:
            with self._lock:
                self._events.pop(run_id, None)

    def shutdown(self) -> None:
        """关闭时通知任务协作式取消，并等待 worker 收尾释放数据库连接。"""

        with self._lock:
            self._closed = True
            for event in self._events.values():
                event.set()
        self._executor.shutdown(wait=True, cancel_futures=False)
