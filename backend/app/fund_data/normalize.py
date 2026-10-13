"""将 AKShare 不稳定的字段映射为基金行情领域类型。"""

import math
import re
from datetime import date, datetime
from typing import Any

from app.fund_data.models import (
    FundBar, FundDataError, FundDistribution, FundInstrument, FundIntradayBar,
    FundNavPoint, FundQuote, FundType,
)


def _records(value: Any) -> list[dict[str, Any]]:
    """兼容 AKShare DataFrame 与测试/缓存使用的映射列表。"""

    if value is None:
        return []
    if hasattr(value, "iterrows"):
        return [row.to_dict() for _, row in value.iterrows()]
    if isinstance(value, dict):
        return [value]
    return list(value)


def _pick(row: dict[str, Any], *names: str) -> Any:
    return next((row[name] for name in names if name in row and row[name] is not None), None)


def _number(value: Any) -> float | None:
    """解析数值列；空白、横线和非有限数都视为缺失。"""

    if value is None:
        return None
    raw = str(value).strip().replace(",", "")
    if raw.lower() in {"", "-", "nan", "nat", "none", "null"}:
        return None
    if raw.endswith("%"):
        raw = raw[:-1]
    try:
        result = float(raw)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    try:
        return datetime.strptime(raw, "%Y%m%d").date() if len(raw) == 8 and raw.isdigit() else date.fromisoformat(raw[:10].replace("/", "-"))
    except ValueError:
        return None


def _symbol(row: dict[str, Any]) -> str:
    code = str(_pick(row, "基金代码", "代码", "code", "symbol") or "").strip()
    if code[:2].lower() in {"sh", "sz", "bj"}:
        code = code[2:]
    if code.isdigit() and len(code) <= 6:
        code = code.zfill(6)
    return code if len(code) == 6 and code.isdigit() else ""


def normalize_spot(
    rows: Any,
    fund_type: FundType,
    fetched_at: datetime | None = None,
    volume_multiplier: float = 100,
) -> list[FundQuote]:
    """标准化 ETF/LOF 场内快照；成交量从手换成份，涨幅转成小数。"""

    if fund_type == "open_end":
        raise FundDataError("开放式基金应使用公布净值快照")
    stamp = fetched_at or datetime.now().astimezone()
    output: list[FundQuote] = []
    for row in _records(rows):
        symbol = _symbol(row)
        name = str(_pick(row, "名称", "基金简称", "name") or "").strip()
        price = _number(_pick(row, "最新价", "最新", "close"))
        if not symbol or not name or price is None or price <= 0:
            continue
        change = _number(_pick(row, "涨跌幅", "涨跌幅(%)", "change_percent")) or 0
        volume = _number(_pick(row, "成交量", "volume")) or 0
        output.append(FundQuote(
            symbol, name, fund_type, price, change / 100,
            _number(_pick(row, "今开", "开盘价", "开盘")),
            _number(_pick(row, "最高", "最高价")),
            _number(_pick(row, "最低", "最低价")),
            _number(_pick(row, "昨收", "昨日收盘")), volume * volume_multiplier,
            _number(_pick(row, "成交额", "成交金额", "turnover")) or 0,
            "AKShare 东方财富", stamp,
        ))
    return sorted(output, key=lambda item: item.symbol)


def normalize_open_fund_snapshot(
    rows: Any,
    fetched_at: datetime | None = None,
    categories_rows: Any = None,
) -> tuple[list[FundInstrument], list[FundQuote]]:
    """保留有正数已公布单位净值的开放式基金，并标出申赎状态。"""

    stamp = fetched_at or datetime.now().astimezone()
    categories = {
        _symbol(row): str(_pick(row, "基金类型", "类型") or "").strip()
        for row in _records(categories_rows)
    }
    instruments: list[FundInstrument] = []
    quotes: list[FundQuote] = []
    for row in _records(rows):
        symbol = _symbol(row)
        name = str(_pick(row, "基金简称", "基金名称", "名称", "name") or "").strip()
        nav = _number(_pick(row, "单位净值", "最新净值", "净值"))
        nav_date = _day(_pick(row, "日期", "净值日期"))
        if not symbol or not name or nav is None or nav <= 0 or nav_date is None:
            continue
        status = str(_pick(row, "申购状态", "申购", "申购状态说明") or "未知").strip()
        redemption = str(_pick(row, "赎回状态", "赎回", "赎回状态说明") or "未知").strip()
        fee = str(_pick(row, "手续费", "申购费率", "费用") or "").strip()
        category = categories.get(symbol, "")
        instruments.append(FundInstrument(symbol, name, "open_end", status, redemption, fee, category))
        change = _number(_pick(row, "日增长率", "日涨幅", "增长率")) or 0
        quotes.append(FundQuote(
            symbol, name, "open_end", nav, change / 100, None, None, None, None, 0, 0,
            "AKShare 东方财富基金净值", stamp, nav_date,
            _number(_pick(row, "累计净值", "累计单位净值")), status, redemption, fee,
            category=category,
        ))
    return instruments, sorted(quotes, key=lambda item: item.symbol)


def normalize_bars(rows: Any) -> list[FundBar]:
    """拒绝日期重复或倒序的行情，跳过缺价或非正价记录。"""

    output: list[FundBar] = []
    seen: set[date] = set()
    previous: date | None = None
    for row in _records(rows):
        day = _day(_pick(row, "日期", "净值日期", "date"))
        if day is None:
            raise FundDataError("基金历史行情日期缺失或格式不正确")
        if day in seen:
            raise FundDataError(f"基金历史行情存在重复日期：{day}")
        if previous is not None and day < previous:
            raise FundDataError("基金历史行情日期必须按时间递增")
        seen.add(day)
        previous = day
        prices = [_number(_pick(row, *names)) for names in (
            ("开盘", "开盘价", "open"), ("最高", "最高价", "high"),
            ("最低", "最低价", "low"), ("收盘", "最新价", "close"),
        )]
        if any(price is None or price <= 0 for price in prices):
            continue
        open_price, high, low, close = prices
        if high < max(open_price, close, low) or low > min(open_price, close):
            continue
        volume = _number(_pick(row, "成交量", "volume")) or 0
        turnover = _number(_pick(row, "成交额", "成交金额", "amount")) or 0
        if volume < 0 or turnover < 0:
            continue
        output.append(FundBar(day, open_price, high, low, close, volume * 100, turnover))  # type: ignore[arg-type]
    return output


def normalize_intraday(rows: Any) -> list[FundIntradayBar]:
    """标准化近期一分钟行情并把成交量统一为份。"""

    output: dict[datetime, FundIntradayBar] = {}
    for row in _records(rows):
        raw = _pick(row, "时间", "日期", "trade_time", "day")
        try:
            stamp = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw).strip())
        except (TypeError, ValueError):
            continue
        prices = [_number(_pick(row, *names)) for names in (
            ("开盘", "open"), ("最高", "high"), ("最低", "low"), ("收盘", "close"),
        )]
        if any(price is None or price <= 0 for price in prices):
            continue
        open_price, high, low, close = prices
        if high < max(open_price, close, low) or low > min(open_price, close):
            continue
        volume = _number(_pick(row, "成交量", "volume")) or 0
        turnover = _number(_pick(row, "成交额", "成交金额", "amount")) or 0
        if volume < 0 or turnover < 0:
            continue
        output[stamp] = FundIntradayBar(stamp, open_price, high, low, close, volume * 100, turnover)  # type: ignore[arg-type]
    return [output[stamp] for stamp in sorted(output)]


def normalize_nav(rows: Any, accumulated_rows: Any = None) -> list[FundNavPoint]:
    """读取单位净值曲线；不把累计净值当作交易执行价格。"""

    accumulated_by_date: dict[date, float] = {}
    for row in _records(accumulated_rows):
        accumulated_day = _day(_pick(row, "净值日期", "日期"))
        accumulated = _number(_pick(row, "累计净值", "累计净值(元)"))
        if accumulated_day is not None and accumulated is not None and accumulated > 0:
            accumulated_by_date[accumulated_day] = accumulated
    output: list[FundNavPoint] = []
    seen: set[date] = set()
    previous: date | None = None
    for row in _records(rows):
        day = _day(_pick(row, "净值日期", "日期", "净值增长率日期"))
        if day is None or day in seen or (previous is not None and day < previous):
            raise FundDataError("基金净值历史存在无效、重复或倒序日期")
        seen.add(day)
        previous = day
        nav = _number(_pick(row, "单位净值", "单位净值(元)"))
        if nav is None or nav <= 0:
            continue
        change = _number(_pick(row, "日增长率", "日增长率(%)"))
        accumulated = _number(_pick(row, "累计净值", "累计净值(元)")) or accumulated_by_date.get(day)
        output.append(FundNavPoint(day, nav, accumulated, change / 100 if change is not None else None))
    return output


def normalize_distributions(rows: Any) -> list[FundDistribution]:
    """只记录可确认除息日和正数现金分红的事件。"""

    output: list[FundDistribution] = []
    for index, row in enumerate(_records(rows)):
        day = _day(_pick(row, "除息日", "除权日", "权益登记日"))
        raw_cash = _pick(row, "每份分红", "分红", "派息", "现金分红")
        cash = _number(raw_cash)
        if cash is None and raw_cash is not None:
            # 官方历史分红字段示例为“每份派现金0.0050元”，提取其中的现金金额。
            match = re.search(r"\d+(?:\.\d+)?", str(raw_cash).replace(",", ""))
            cash = _number(match.group(0)) if match else None
        if day is None or cash is None or cash <= 0:
            continue
        output.append(FundDistribution(day, cash, f"{day.isoformat()}|{cash:.8f}|{index}"))
    return output
