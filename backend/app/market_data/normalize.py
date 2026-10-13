"""将 AKShare DataFrame/记录映射为稳定的领域类型并校验关键字段。"""

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import Bar, CorporateAction, IntradayBar, Quote

logger = logging.getLogger(__name__)
# Windows Python 环境可能没有系统 IANA 时区库；中国固定使用 UTC+8，无夏令时。
try:
    _CHINA_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    _CHINA_TZ = timezone(timedelta(hours=8), name="UTC+08:00")


class MarketDataError(ValueError):
    """行情字段缺失、格式变化或业务数据不可用。"""


def _records(value: Any) -> list[dict[str, Any]]:
    """兼容 AKShare 返回的 DataFrame 和测试/缓存层使用的字典列表。"""

    if value is None:
        return []
    if hasattr(value, "iterrows"):
        return [row.to_dict() for _, row in value.iterrows()]
    if isinstance(value, dict):
        return [value]
    return list(value)


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None or str(value).strip().lower() in {"", "-", "nan", "nat", "none"}:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _date(value: Any) -> date | None:
    if value is None or str(value).strip().lower() in {"", "-", "nan", "nat", "none"}:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return datetime.fromisoformat(text[:10].replace("/", "-")).date()
    except ValueError:
        return None


def _index_by_date(rows: list[dict[str, Any]], label: str) -> dict[date, dict[str, Any]]:
    result: dict[date, dict[str, Any]] = {}
    for row in rows:
        day = _date(row.get("日期"))
        if day is None:
            raise MarketDataError(f"{label}行情日期为空或格式不正确")
        if day in result:
            raise MarketDataError(f"{label}行情存在重复交易日：{day}")
        result[day] = row
    return result


def normalize_bars(symbol: str, raw_rows: Any, adjusted_rows: Any) -> list[Bar]:
    """按日期合并原始和前复权 OHLC，确保信号/成交价格来源不会混用。"""

    raw = _index_by_date(_records(raw_rows), "未复权")
    adjusted = _index_by_date(_records(adjusted_rows), "前复权")
    if not raw:
        raise MarketDataError(f"{symbol} 没有可用历史行情")
    if set(raw) != set(adjusted):
        raise MarketDataError(f"{symbol} 未复权与前复权行情日期不一致")

    output: list[Bar] = []
    for day in sorted(raw):
        original = raw[day]
        qfq = adjusted[day]
        values = (
            _number(original.get("开盘")), _number(original.get("最高")),
            _number(original.get("最低")), _number(original.get("收盘")),
            _number(qfq.get("开盘")), _number(qfq.get("最高")),
            _number(qfq.get("最低")), _number(qfq.get("收盘")),
        )
        if any(value is None or value <= 0 for value in values):
            raise MarketDataError(f"{symbol} {day} 的 OHLC 缺失或非正数")
        output.append(
            Bar(
                trade_date=day,
                open=values[0], high=values[1], low=values[2], close=values[3],  # type: ignore[arg-type]
                adj_open=values[4], adj_high=values[5], adj_low=values[6], adj_close=values[7],  # type: ignore[arg-type]
                volume=_number(original.get("成交量"), 0) or 0,
                turnover=_number(original.get("成交额"), 0) or 0,
            )
        )
    return output


def normalize_intraday_bars(rows: Any, volume_multiplier: float = 1) -> list[IntradayBar]:
    """统一分钟字段和时区，跳过上游返回的空行或无效 OHLC。"""

    output: dict[datetime, IntradayBar] = {}
    for row in _records(rows):
        raw_time = row.get("trade_time", row.get("时间", row.get("day")))
        try:
            trade_time = raw_time if isinstance(raw_time, datetime) else datetime.fromisoformat(str(raw_time).strip())
        except (TypeError, ValueError):
            continue
        trade_time = trade_time.replace(tzinfo=_CHINA_TZ) if trade_time.tzinfo is None else trade_time.astimezone(_CHINA_TZ)
        prices = tuple(_number(row.get(chinese, row.get(english))) for chinese, english in (
            ("开盘", "open"), ("最高", "high"), ("最低", "low"), ("收盘", "close"),
        ))
        if any(value is None or value <= 0 for value in prices):
            continue
        open_price, high, low, close = prices
        if high < max(open_price, close, low) or low > min(open_price, close):
            continue
        volume = _number(row.get("成交量", row.get("volume")), 0) or 0
        turnover = _number(row.get("成交额", row.get("amount")), 0) or 0
        if volume < 0 or turnover < 0:
            continue
        output[trade_time] = IntradayBar(
            trade_time=trade_time,
            open=open_price, high=high, low=low, close=close,  # type: ignore[arg-type]
            volume=volume * volume_multiplier,
            turnover=turnover,
        )
    return [output[stamp] for stamp in sorted(output)]


def normalize_quotes(
    rows: Any,
    fetched_at: datetime,
    source: str = "AKShare 东方财富",
) -> list[Quote]:
    """标准化全市场快照；无有效最新价的停牌/预开盘记录不伪造为 0 元。"""

    output: list[Quote] = []
    for row in _records(rows):
        symbol = str(row.get("代码", "")).strip().zfill(6)
        name = str(row.get("名称", "")).strip()
        latest = _number(row.get("最新价"))
        if len(symbol) != 6 or not symbol.isdigit() or not name or latest is None or latest <= 0:
            continue
        output.append(
            Quote(
                symbol=symbol,
                name=name,
                latest_price=latest,
                change_amount=_number(row.get("涨跌额"), 0) or 0,
                change_percent=_number(row.get("涨跌幅"), 0) or 0,
                open=_number(row.get("今开"), 0) or 0,
                high=_number(row.get("最高"), 0) or 0,
                low=_number(row.get("最低"), 0) or 0,
                previous_close=_number(row.get("昨收"), 0) or 0,
                volume_hands=_number(row.get("成交量"), 0) or 0,
                turnover=_number(row.get("成交额"), 0) or 0,
                source=source,
                fetched_at=fetched_at,
            )
        )
    return sorted(output, key=lambda item: item.symbol)


def normalize_actions(symbol: str, rows: Any) -> tuple[list[CorporateAction], list[str]]:
    """将巨潮“每 10 股”分红送转换算为每股权益，并显式标记未确认日期。"""

    actions: list[CorporateAction] = []
    warnings: list[str] = []
    for index, row in enumerate(_records(rows)):
        record_date = _date(row.get("股权登记日"))
        ex_date = _date(row.get("除权日"))
        cash_per_share = (_number(row.get("派息比例"), 0) or 0) / 10
        bonus_per_share = (
            (_number(row.get("送股比例"), 0) or 0) + (_number(row.get("转增比例"), 0) or 0)
        ) / 10
        implemented = record_date is not None and ex_date is not None and record_date <= ex_date
        status = "implemented" if implemented else "unresolved"
        if not implemented:
            warnings.append(f"{symbol} 的第 {index + 1} 条分红配送记录缺少有效登记日/除权日，回测未计入。")
        if cash_per_share < 0 or bonus_per_share < 0:
            warnings.append(f"{symbol} 的第 {index + 1} 条分红配送数据为负数，回测未计入。")
            status = "unresolved"
        source_key = "|".join(
            (
                str(row.get("报告时间", "")),
                record_date.isoformat() if record_date else "",
                ex_date.isoformat() if ex_date else "",
                f"{cash_per_share:.8f}",
                f"{bonus_per_share:.8f}",
            )
        )
        actions.append(
            CorporateAction(
                symbol=symbol,
                record_date=record_date,
                ex_date=ex_date,
                cash_per_share=max(0, cash_per_share),
                bonus_shares_per_share=max(0, bonus_per_share),
                status=status,
                source_key=source_key,
            )
        )
    return actions, warnings
