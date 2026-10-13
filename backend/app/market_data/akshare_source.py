"""AKShare 外部接口适配；封装调用、有限重试、超时和字段标准化。"""

import logging
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

from app.config import MarketDataSettings
from app.market_data.normalize import (
    MarketDataError, normalize_actions, normalize_bars, normalize_intraday_bars, normalize_quotes,
)

logger = logging.getLogger(__name__)
_REQUEST_PATCH_LOCK = threading.Lock()


class MarketDataSourceError(RuntimeError):
    """AKShare 无法返回可用数据。"""


@contextmanager
def _requests_timeout(seconds: int) -> Iterator[None]:
    """为 AKShare 内部 requests 调用补上配置超时，避免网络请求无限等待。"""

    try:
        import requests
    except ImportError:
        yield
        return

    # AKShare 的公开接口不暴露 timeout 参数；在锁内临时包装 requests，完成后恢复原实现。
    with _REQUEST_PATCH_LOCK:
        original = requests.sessions.Session.request

        def bounded_request(session: Any, method: str, url: str, **kwargs: Any) -> Any:
            # AKShare 有些接口显式传 timeout=None；此时也必须使用配置超时。
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = seconds
            return original(session, method, url, **kwargs)

        requests.sessions.Session.request = bounded_request
        try:
            yield
        finally:
            requests.sessions.Session.request = original


class AKShareSource:
    """对历史 K 线、实时快照与分红配送接口提供标准化返回值。"""

    def __init__(self, settings: MarketDataSettings, provider: Any | None = None) -> None:
        self._settings = settings
        self._provider = provider

    def _akshare(self) -> Any:
        if self._provider is None:
            try:
                import akshare
            except ImportError as exc:
                raise MarketDataSourceError("缺少 AKShare，请按 requirements.txt 安装后重试") from exc
            self._provider = akshare
        return self._provider

    def _call(self, method_name: str, **kwargs: object) -> Any:
        provider = self._akshare()
        method = getattr(provider, method_name, None)
        if method is None:
            raise MarketDataSourceError(f"当前 AKShare 版本不包含接口 {method_name}")
        for attempt in range(self._settings.max_retries + 1):
            try:
                with _requests_timeout(self._settings.request_timeout_seconds):
                    return method(**kwargs)
            except Exception as exc:
                if attempt >= self._settings.max_retries:
                    logger.exception("AKShare 接口 %s 调用失败", method_name)
                    raise MarketDataSourceError(f"AKShare 接口 {method_name} 暂时不可用") from exc
                logger.warning("AKShare 接口 %s 失败，进行第 %s 次重试", method_name, attempt + 1)
                time.sleep(min(0.2 * (attempt + 1), 1))
        raise MarketDataSourceError(f"AKShare 接口 {method_name} 暂时不可用")

    def fetch_bars(self, symbol: str, start_date: date, end_date: date):
        """优先取东方财富日线；不可用时用腾讯日线补位，再按日期合并复权价格。"""

        common = {
            "symbol": symbol,
            "period": "daily",
            "start_date": start_date.strftime("%Y%m%d"),
            "end_date": end_date.strftime("%Y%m%d"),
        }
        try:
            raw = self._call("stock_zh_a_hist", **common, adjust="")
            adjusted = self._call("stock_zh_a_hist", **common, adjust="qfq")
            return normalize_bars(symbol, raw, adjusted)
        except (MarketDataSourceError, MarketDataError):
            logger.warning("AKShare 东方财富日线不可用，切换到腾讯日线接口")

        tx_common = {
            "symbol": symbol,
            "start_date": common["start_date"],
            "end_date": common["end_date"],
        }
        try:
            raw = self._call("stock_zh_a_hist_tx", **tx_common, adjust="")
            adjusted = self._call("stock_zh_a_hist_tx", **tx_common, adjust="qfq")
            return normalize_bars(
                symbol,
                self._rename_tx_bars(raw),
                self._rename_tx_bars(adjusted),
            )
        except (MarketDataSourceError, MarketDataError) as exc:
            logger.exception("AKShare 腾讯日线接口也不可用")
            raise MarketDataSourceError("AKShare 东方财富和腾讯历史行情接口暂时不可用") from exc

    def fetch_quote(self, symbol: str):
        """从腾讯全市场快照中取单股数据；完整快照由上层 Redis 缓存复用。"""

        quote = next((item for item in self.fetch_quotes() if item.symbol == symbol), None)
        if quote is None:
            raise MarketDataSourceError(f"AKShare 没有返回 {symbol} 的有效实时行情")
        return quote

    def fetch_intraday_bars(self, symbol: str, trade_date: date):
        """优先取东方财富当日一分钟线；不可用时改用新浪并统一成交量为股。"""

        start_time = f"{trade_date.isoformat()} 09:15:00"
        end_time = f"{trade_date.isoformat()} 15:00:00"
        errors: list[MarketDataSourceError] = []
        try:
            rows = self._call(
                "stock_zh_a_hist_min_em", symbol=symbol, start_date=start_time,
                end_date=end_time, period="1", adjust="",
            )
            bars = normalize_intraday_bars(rows, volume_multiplier=100)
            if bars:
                return bars
        except (MarketDataSourceError, MarketDataError) as exc:
            errors.append(exc if isinstance(exc, MarketDataSourceError) else MarketDataSourceError(str(exc)))
            logger.warning("AKShare 东方财富分钟行情不可用，切换到新浪分钟行情")

        prefix = "sh" if symbol.startswith(("60", "68", "90")) else "sz" if symbol.startswith(("00", "30")) else "bj"
        try:
            rows = self._call("stock_zh_a_minute", symbol=f"{prefix}{symbol}", period="1", adjust="")
            bars = normalize_intraday_bars(rows)
            if bars:
                return bars
        except (MarketDataSourceError, MarketDataError) as exc:
            errors.append(exc if isinstance(exc, MarketDataSourceError) else MarketDataSourceError(str(exc)))

        if len(errors) == 2:
            logger.error("AKShare 东方财富和新浪分钟行情接口都不可用：%s", errors[-1])
            raise MarketDataSourceError("AKShare 东方财富和新浪分钟行情接口暂时不可用") from errors[-1]
        return []

    def fetch_actions(self, symbol: str):
        """读取并标准化巨潮历史分红；未能确定日期的事件附带提示。"""

        rows = self._call("stock_dividend_cninfo", symbol=symbol)
        return normalize_actions(symbol, rows)

    def fetch_quotes(self):
        """获取腾讯全市场快照，供单股行情与搜索共用 Redis 缓存。"""

        rows = self._call("stock_zh_a_spot_tx")
        return normalize_quotes(
            self._rename_tx_quotes(rows),
            datetime.now().astimezone(),
            source="AKShare 腾讯",
        )

    @staticmethod
    def _rename_tx_quotes(rows: Any) -> list[dict[str, Any]]:
        """将腾讯实时列表映射到领域字段，并统一成交额单位为元。"""

        if hasattr(rows, "iterrows"):
            records = [row.to_dict() for _, row in rows.iterrows()]
        elif isinstance(rows, dict):
            records = [rows]
        else:
            records = rows

        output: list[dict[str, Any]] = []
        for row in records:
            code = str(row.get("code", "")).strip()
            # 腾讯 code 形如 sh600000；领域代码只保存六位数字。
            symbol = code[-6:] if len(code) >= 6 else code
            if len(symbol) != 6 or not symbol.isdigit():
                continue
            try:
                latest = float(row.get("zxj"))
                change = float(row.get("zd"))
                previous_close = round(latest - change, 6)
            except (TypeError, ValueError):
                previous_close = None
            try:
                # 腾讯列表的成交额单位为万元，统一换算为 Quote 使用的元。
                turnover = float(row.get("turnover")) * 10000
            except (TypeError, ValueError):
                turnover = None
            output.append({
                "代码": symbol,
                "名称": row.get("name"),
                "最新价": row.get("zxj"),
                "涨跌额": row.get("zd"),
                "涨跌幅": row.get("zdf"),
                # 该腾讯接口不返回当日 OHLC；由页面显示为“—”，不伪装成 0 元。
                "今开": None,
                "最高": None,
                "最低": None,
                "昨收": previous_close,
                # 腾讯 volume 按“手”返回，与 Quote.volume_hands 的单位一致。
                "成交量": row.get("volume"),
                "成交额": turnover,
            })
        return output

    @staticmethod
    def _rename_tx_bars(rows: Any) -> Any:
        """把腾讯接口的小写列名映射为领域标准化器使用的 AKShare 列名。"""

        columns = {
            "date": "日期", "open": "开盘", "high": "最高", "low": "最低",
            "close": "收盘", "volume": "成交量", "amount": "成交额",
        }
        if hasattr(rows, "rename"):
            return rows.rename(columns=columns)
        records = [rows] if isinstance(rows, dict) else rows
        return [{target: record.get(source) for source, target in columns.items()} for record in records]

