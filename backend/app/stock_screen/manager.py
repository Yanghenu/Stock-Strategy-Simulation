"""选股实验独立 worker；SQL 检查点保证进程重启后接着处理。"""

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

from app.config import StockScreenSettings
from app.market_data.service import MarketDataService
from app.market_data.service import MarketDataServiceError
from app.stock_screen.repository import StockScreenRepository
from app.stock_screen.rules import score_symbol

logger = logging.getLogger(__name__)
CHINA_TZ = timezone(timedelta(hours=8))


class StockScreenManager:
    def __init__(self, repository: StockScreenRepository, market: MarketDataService,
                 settings: StockScreenSettings, clock: Callable[[], datetime] | None = None) -> None:
        self._repository = repository
        self._market = market
        self._settings = settings
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._clock = clock or (lambda: datetime.now(CHINA_TZ))

    def start(self) -> None:
        """单 worker 在数据库就绪后启动；无需内存保存运行任务。"""

        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="stock-screen-worker", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def create(self, limit: int | None = None) -> str:
        """收盘后固定股票池；开始之前先验证全市场快照，失败不创建空任务。"""

        now = self._clock().astimezone(CHINA_TZ)
        if now.weekday() < 5 and now.time() < time(16, 0):
            raise ValueError("请在 A 股收盘后 16:00 再启动扫描，确保使用完整日线与成交额")
        count = self._settings.default_universe_size if limit is None else limit
        if count < 1 or count > self._settings.max_universe_size:
            raise ValueError(f"扫描数量必须为 1 至 {self._settings.max_universe_size}")
        quotes = self._market.get_screening_universe(count, self._settings.minimum_turnover)
        if not quotes:
            raise ValueError("当前行情快照没有满足成交额门槛的股票")
        run_id = str(uuid4())
        self._repository.create_run(run_id, now.date(), self._settings.candidate_count, quotes)
        self._wake.set()
        return run_id

    def list_runs(self) -> list[dict]:
        return self._repository.list_runs()

    def get_run(self, run_id: str) -> dict | None:
        record = self._repository.get_run(run_id)
        if record is None:
            return None
        items = record.pop("items")
        outcomes = record.pop("outcomes")
        completed = sum(item["status"] != "pending" for item in items)
        eligible = [item for item in items if item["status"] == "eligible"]
        candidates = [item for item in eligible if item["momentum_rank"] is not None or item["reversal_rank"] is not None]
        candidates.sort(key=lambda item: (min(item["momentum_rank"] or 999, item["reversal_rank"] or 999), item["symbol"]))
        outcome_by_key = {(item["symbol"], item["horizon_days"]): item for item in outcomes}
        for item in candidates:
            item["outcomes"] = [outcome_by_key[(item["symbol"], horizon)] for horizon in (5, 20)
                                if (item["symbol"], horizon) in outcome_by_key]
        metrics: list[dict] = []
        for horizon in (5, 20):
            benchmark = [float(outcome_by_key[(item["symbol"], horizon)]["return_rate"])
                         for item in eligible if (item["symbol"], horizon) in outcome_by_key]
            benchmark_mean = sum(benchmark) / len(benchmark) if benchmark else None
            for strategy, rank_key in (("momentum_trend", "momentum_rank"), ("relative_reversal", "reversal_rank")):
                selected = [float(outcome_by_key[(item["symbol"], horizon)]["return_rate"])
                            for item in eligible if item[rank_key] is not None and
                            (item["symbol"], horizon) in outcome_by_key]
                metrics.append({
                    "strategy_code": strategy, "horizon_days": horizon,
                    "mean_return": sum(selected) / len(selected) if selected else None,
                    "benchmark_return": benchmark_mean,
                    "win_rate": sum(value > 0 for value in selected) / len(selected) if selected else None,
                    "sample_count": len(selected), "benchmark_count": len(benchmark),
                })
        return {**record, "processed_count": completed, "eligible_count": len(eligible),
                "excluded_count": sum(item["status"] == "excluded" for item in items),
                "candidates": candidates, "metrics": metrics}

    def cancel(self, run_id: str) -> bool:
        cancelled = self._repository.cancel_run(run_id)
        if cancelled:
            self._wake.set()
        return cancelled

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run = self._repository.next_active_run()
                if run:
                    self._process_run(run)
                elif self._clock().astimezone(CHINA_TZ).time() >= time(16, 0):
                    self._evaluate_due()
            except Exception:
                logger.exception("选股 worker 处理失败；下次轮询会从 SQL 检查点恢复")
            self._wake.wait(self._settings.worker_interval_seconds)
            self._wake.clear()

    def _process_run(self, run: dict) -> None:
        run_id = str(run["run_id"])
        self._repository.set_running(run_id)
        while not self._stop.is_set():
            item = self._repository.next_pending_item(run_id)
            if item is None:
                self._repository.finalize_run(run_id, int(run["candidate_count"]))
                return
            symbol = item["symbol"]
            try:
                # 约 18 个月日历日覆盖 261 根交易日；不读取截止日之后的价格。
                cutoff = run["signal_cutoff"]
                bars = self._market.get_bars(symbol, cutoff - timedelta(days=550), cutoff)
                score = score_symbol(bars, cutoff)
                self._repository.save_signal(run_id, symbol, score)
            except MarketDataServiceError as exc:
                if "没有历史行情" in str(exc):
                    # 无上市日线是单股数据覆盖不足，不阻塞整批任务。
                    self._repository.save_signal(run_id, symbol, None)
                else:
                    logger.warning("选股扫描 %s 股票 %s 暂未完成：%s", run_id, symbol, exc)
                    self._repository.set_source_error(run_id, f"{symbol} 日线暂不可用：{exc}")
                    return
            except Exception as exc:
                # 外部源暂不可用时保留 pending，不把缺失数据误算为弱股票。
                logger.warning("选股扫描 %s 股票 %s 暂未完成：%s", run_id, symbol, exc)
                self._repository.set_source_error(run_id, f"{symbol} 日线暂不可用：{exc}")
                return
            if self._stop.wait(self._settings.request_gap_seconds):
                return

    def _evaluate_due(self) -> None:
        """每日只处理一小批到期股票；历史调整价格用同一次拉取计算相对收益。"""

        today = self._clock().astimezone(CHINA_TZ).date()
        for item in self._repository.due_evaluations(today, limit=20):
            if self._stop.is_set():
                return
            try:
                bars = self._market.get_bars(item["symbol"], item["signal_date"], today)
                by_date = {bar.trade_date: bar for bar in bars}
                origin = by_date.get(item["signal_date"])
                future = sorted((bar for bar in bars if item["signal_date"] < bar.trade_date <= today),
                                key=lambda bar: bar.trade_date)
                outcomes = []
                if origin and origin.adj_close > 0:
                    for horizon in (5, 20):
                        if len(future) >= horizon and future[horizon - 1].adj_close > 0:
                            target = future[horizon - 1]
                            outcomes.append((horizon, target.trade_date, target.adj_close,
                                             target.adj_close / origin.adj_close - 1))
                self._repository.save_evaluation(str(item["run_id"]), item["symbol"], today, outcomes)
            except Exception:
                # 网络或数据库失败保留上次尝试日，下一轮可以恢复，不造出零收益。
                logger.exception("选股后续表现读取失败：%s %s", item["run_id"], item["symbol"])
                return
            if self._stop.wait(self._settings.request_gap_seconds):
                return
