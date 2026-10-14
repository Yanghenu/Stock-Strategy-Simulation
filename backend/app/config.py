"""集中加载 ini 配置，避免运行参数散落在 API、行情服务和回测代码中。"""

import configparser
import math
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DatabaseSettings:
    server: str
    database: str
    authentication: str
    username: str
    password: str
    encrypt: bool
    trust_server_certificate: bool
    connection_timeout_seconds: int
    statement_timeout_seconds: int


@dataclass(frozen=True)
class RedisSettings:
    host: str
    port: int
    db: int
    password: str
    socket_timeout_seconds: float
    cache_ttl_seconds: int
    refresh_interval_seconds: int
    refresh_lock_seconds: int


@dataclass(frozen=True)
class MarketDataSettings:
    request_timeout_seconds: int
    max_retries: int
    # 仅驱动行情页当日 K 线快照，不改变全市场或纸面实验刷新频率。
    chart_refresh_interval_seconds: int = 10
    # 分时数据单独配置，默认两秒刷新；实际新数据仍受上游分钟线更新节奏限制。
    intraday_refresh_interval_seconds: int = 2


@dataclass(frozen=True)
class BacktestDefaults:
    initial_capital: float
    short_window: int
    long_window: int
    commission_rate: float
    minimum_commission: float
    stamp_duty_rate: float
    slippage_rate: float


@dataclass(frozen=True)
class PaperTradingSettings:
    """前瞻纸面实验参数；刷新间隔沿用 Redis 行情刷新配置。"""

    symbols: tuple[str, ...] = ("600000", "600028", "600887", "600276", "000333", "601899")
    ma_period: int = 60
    breakout_period: int = 20
    atr_period: int = 20
    atr_multiplier: float = 2.0


@dataclass(frozen=True)
class FundSettings:
    """基金行情与独立纸面实验的刷新、资金和费用默认值。"""

    exchange_refresh_interval_seconds: int = 10
    open_nav_refresh_interval_seconds: int = 300
    worker_interval_seconds: int = 60
    default_initial_capital: float = 100000
    commission_rate: float = 0.0003
    minimum_commission: float = 5
    stamp_duty_rate: float = 0
    slippage_rate: float = 0.0005
    subscription_fee_rate: float = 0.0015
    redemption_fee_rate: float = 0.005


@dataclass(frozen=True)
class StockScreenSettings:
    """全市场扫描的有界工作量和研究阈值；与纸面交易刷新配置独立。"""

    default_universe_size: int = 500
    max_universe_size: int = 2000
    minimum_turnover: float = 10000000
    candidate_count: int = 20
    request_gap_seconds: float = 0.3
    worker_interval_seconds: int = 300


@dataclass(frozen=True)
class Settings:
    database: DatabaseSettings
    redis: RedisSettings
    market_data: MarketDataSettings
    backtest: BacktestDefaults
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    paper_trading: PaperTradingSettings = field(default_factory=PaperTradingSettings)
    fund: FundSettings = field(default_factory=FundSettings)
    stock_screen: StockScreenSettings = field(default_factory=StockScreenSettings)


def _positive(section: configparser.SectionProxy, key: str, integer: bool = True) -> int | float:
    value = section.getint(key) if integer else section.getfloat(key)
    if value <= 0:
        raise ValueError(f"配置项 {key} 必须大于 0")
    return value


def load_settings(path: str | Path | None = None) -> Settings:
    """优先读取本机 config.ini；开发环境缺少时回退到无秘密的模板。"""

    backend_dir = Path(__file__).resolve().parents[1]
    config_path = Path(path) if path is not None else backend_dir / "config.ini"
    if path is None and not config_path.exists():
        config_path = backend_dir / "config.ini.example"
    if not config_path.is_file():
        raise FileNotFoundError(f"找不到配置文件：{config_path}")

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    required = ("database", "redis", "market_data", "backtest")
    missing = [name for name in required if name not in parser]
    if missing:
        raise ValueError(f"配置文件缺少区段：{', '.join(missing)}")

    db = parser["database"]
    authentication = db.get("authentication", "windows").strip().lower()
    if authentication not in {"windows", "sql"}:
        raise ValueError("database.authentication 仅支持 windows 或 sql")
    username = db.get("username", "").strip()
    password = db.get("password", "")
    if authentication == "sql" and (not username or not password):
        raise ValueError("SQL Server 使用 SQL 认证时必须配置用户名和密码")
    server = db.get("server", "").strip()
    database = db.get("database", "").strip()
    if not server or not database:
        raise ValueError("必须配置 SQL Server 实例和已存在的数据库名")

    redis_section = parser["redis"]
    redis_settings = RedisSettings(
        host=redis_section.get("host", "127.0.0.1").strip(),
        port=redis_section.getint("port", 6379),
        db=redis_section.getint("db", 0),
        password=redis_section.get("password", ""),
        socket_timeout_seconds=_positive(redis_section, "socket_timeout_seconds", integer=False),
        cache_ttl_seconds=int(_positive(redis_section, "cache_ttl_seconds")),
        refresh_interval_seconds=int(_positive(redis_section, "refresh_interval_seconds")),
        refresh_lock_seconds=int(_positive(redis_section, "refresh_lock_seconds")),
    )
    if not redis_settings.host or not 1 <= redis_settings.port <= 65535 or redis_settings.db < 0:
        raise ValueError("Redis 地址、端口或数据库编号无效")
    if redis_settings.cache_ttl_seconds < redis_settings.refresh_interval_seconds:
        raise ValueError("Redis 快照 TTL 不能小于行情刷新间隔")

    market = parser["market_data"]
    chart_refresh_interval = market.getint("chart_refresh_interval_seconds", fallback=10)
    if chart_refresh_interval < 5:
        raise ValueError("配置项 chart_refresh_interval_seconds 不能小于 5 秒")
    intraday_refresh_interval = market.getint("intraday_refresh_interval_seconds", fallback=2)
    if intraday_refresh_interval < 2:
        raise ValueError("配置项 intraday_refresh_interval_seconds 不能小于 2 秒")
    backtest = parser["backtest"]
    defaults = BacktestDefaults(
        initial_capital=float(_positive(backtest, "default_initial_capital", integer=False)),
        short_window=int(_positive(backtest, "default_short_window")),
        long_window=int(_positive(backtest, "default_long_window")),
        commission_rate=backtest.getfloat("default_commission_rate"),
        minimum_commission=backtest.getfloat("default_minimum_commission"),
        stamp_duty_rate=backtest.getfloat("default_stamp_duty_rate"),
        slippage_rate=backtest.getfloat("default_slippage_rate"),
    )
    if defaults.short_window >= defaults.long_window:
        raise ValueError("默认短均线周期必须小于长均线周期")
    if min(defaults.commission_rate, defaults.minimum_commission, defaults.stamp_duty_rate) < 0:
        raise ValueError("默认费率和最低佣金不能为负数")
    if not 0 <= defaults.slippage_rate < 1:
        raise ValueError("默认滑点比例必须大于等于 0 且小于 1")

    paper = parser["paper_trading"] if "paper_trading" in parser else None
    raw_symbols = paper.get("symbols", ",".join(PaperTradingSettings().symbols)) if paper else ",".join(PaperTradingSettings().symbols)
    symbols = tuple(item.strip() for item in raw_symbols.split(",") if item.strip())
    if not symbols or len(symbols) > 20 or len(set(symbols)) != len(symbols):
        raise ValueError("paper_trading.symbols 必须包含 1 到 20 个不重复股票代码")
    if any(len(item) != 6 or not item.isdigit() for item in symbols):
        raise ValueError("paper_trading.symbols 中的代码必须是六位数字")
    paper_settings = PaperTradingSettings(
        symbols=symbols,
        ma_period=int(_positive(paper, "ma_period")) if paper else 60,
        breakout_period=int(_positive(paper, "breakout_period")) if paper else 20,
        atr_period=int(_positive(paper, "atr_period")) if paper else 20,
        atr_multiplier=float(_positive(paper, "atr_multiplier", integer=False)) if paper else 2.0,
    )
    if paper_settings.breakout_period >= paper_settings.ma_period:
        raise ValueError("纸面策略突破周期必须小于趋势均线周期")

    fund_section = parser["fund"] if "fund" in parser else None
    fund_defaults = FundSettings()
    def fund_int(name: str, default: int) -> int:
        value = fund_section.getint(name, fallback=default) if fund_section else default
        if value <= 0:
            raise ValueError(f"配置项 fund.{name} 必须大于 0")
        return value

    def fund_float(name: str, default: float, positive: bool = False) -> float:
        value = fund_section.getfloat(name, fallback=default) if fund_section else default
        if not math.isfinite(value) or (positive and value <= 0):
            raise ValueError(f"配置项 fund.{name} 必须是有效的正数")
        return value

    fund_settings = FundSettings(
        exchange_refresh_interval_seconds=fund_int("exchange_refresh_interval_seconds", fund_defaults.exchange_refresh_interval_seconds),
        open_nav_refresh_interval_seconds=fund_int("open_nav_refresh_interval_seconds", fund_defaults.open_nav_refresh_interval_seconds),
        worker_interval_seconds=fund_int("worker_interval_seconds", fund_defaults.worker_interval_seconds),
        default_initial_capital=fund_float("default_initial_capital", fund_defaults.default_initial_capital, positive=True),
        commission_rate=fund_float("commission_rate", fund_defaults.commission_rate),
        minimum_commission=fund_float("minimum_commission", fund_defaults.minimum_commission),
        stamp_duty_rate=fund_float("stamp_duty_rate", fund_defaults.stamp_duty_rate),
        slippage_rate=fund_float("slippage_rate", fund_defaults.slippage_rate),
        subscription_fee_rate=fund_float("subscription_fee_rate", fund_defaults.subscription_fee_rate),
        redemption_fee_rate=fund_float("redemption_fee_rate", fund_defaults.redemption_fee_rate),
    )
    if min(
        fund_settings.commission_rate, fund_settings.minimum_commission, fund_settings.stamp_duty_rate,
        fund_settings.slippage_rate, fund_settings.subscription_fee_rate, fund_settings.redemption_fee_rate,
    ) < 0:
        raise ValueError("基金佣金、税费、滑点和申赎费率不能为负数")
    if max(
        fund_settings.commission_rate, fund_settings.stamp_duty_rate, fund_settings.slippage_rate,
        fund_settings.subscription_fee_rate, fund_settings.redemption_fee_rate,
    ) >= 1:
        raise ValueError("基金费率和滑点必须小于 1")
    if min(fund_settings.exchange_refresh_interval_seconds, fund_settings.worker_interval_seconds) < 5:
        raise ValueError("基金行情和实验刷新间隔不能小于 5 秒")
    if fund_settings.open_nav_refresh_interval_seconds < 60:
        raise ValueError("开放式基金净值刷新间隔不能小于 60 秒")

    # 新区段可选，已有 config.ini / Docker 配置不需要迁移即可按默认值启动。
    screen = parser["stock_screen"] if "stock_screen" in parser else None
    screen_defaults = StockScreenSettings()
    screen_settings = StockScreenSettings(
        default_universe_size=screen.getint("default_universe_size", fallback=screen_defaults.default_universe_size) if screen else screen_defaults.default_universe_size,
        max_universe_size=screen.getint("max_universe_size", fallback=screen_defaults.max_universe_size) if screen else screen_defaults.max_universe_size,
        minimum_turnover=screen.getfloat("minimum_turnover", fallback=screen_defaults.minimum_turnover) if screen else screen_defaults.minimum_turnover,
        candidate_count=screen.getint("candidate_count", fallback=screen_defaults.candidate_count) if screen else screen_defaults.candidate_count,
        request_gap_seconds=screen.getfloat("request_gap_seconds", fallback=screen_defaults.request_gap_seconds) if screen else screen_defaults.request_gap_seconds,
        worker_interval_seconds=screen.getint("worker_interval_seconds", fallback=screen_defaults.worker_interval_seconds) if screen else screen_defaults.worker_interval_seconds,
    )
    if not (1 <= screen_settings.default_universe_size <= screen_settings.max_universe_size <= 3000):
        raise ValueError("stock_screen 股票池数量无效")
    if not (1 <= screen_settings.candidate_count <= screen_settings.default_universe_size):
        raise ValueError("stock_screen 候选股票数量无效")
    if not (math.isfinite(screen_settings.minimum_turnover) and screen_settings.minimum_turnover > 0):
        raise ValueError("stock_screen 最低成交额必须为正数")
    if not (math.isfinite(screen_settings.request_gap_seconds) and 0 <= screen_settings.request_gap_seconds <= 60):
        raise ValueError("stock_screen 请求间隔必须在 0 到 60 秒之间")
    if screen_settings.worker_interval_seconds < 30:
        raise ValueError("stock_screen worker 间隔不能小于 30 秒")

    retries = market.getint("max_retries")
    if retries < 0 or retries > 10:
        raise ValueError("market_data.max_retries 必须在 0 到 10 之间")
    api_host = parser.get("api", "host", fallback="127.0.0.1").strip()
    api_port = parser.getint("api", "port", fallback=8000)
    if api_host.lower() not in {"127.0.0.1", "localhost"}:
        raise ValueError("API 只能绑定本机地址 127.0.0.1 或 localhost")
    if not 1 <= api_port <= 65535:
        raise ValueError("api.port 必须在 1 到 65535 之间")

    return Settings(
        database=DatabaseSettings(
            server=server,
            database=database,
            authentication=authentication,
            username=username,
            password=password,
            encrypt=db.getboolean("encrypt", fallback=True),
            trust_server_certificate=db.getboolean("trust_server_certificate", fallback=True),
            connection_timeout_seconds=int(_positive(db, "connection_timeout_seconds")),
            statement_timeout_seconds=int(_positive(db, "statement_timeout_seconds")),
        ),
        redis=redis_settings,
        market_data=MarketDataSettings(
            request_timeout_seconds=int(_positive(market, "request_timeout_seconds")),
            max_retries=retries,
            chart_refresh_interval_seconds=chart_refresh_interval,
            intraday_refresh_interval_seconds=intraday_refresh_interval,
        ),
        backtest=defaults,
        api_host=api_host,
        api_port=api_port,
        paper_trading=paper_settings,
        fund=fund_settings,
        stock_screen=screen_settings,
    )


def public_config(settings: Settings) -> dict[str, object]:
    """只返回网页表单和轮询所需的安全选项，不暴露连接凭据。"""

    return {
        "refresh_interval_seconds": settings.redis.refresh_interval_seconds,
        "chart_refresh_interval_seconds": settings.market_data.chart_refresh_interval_seconds,
        "intraday_refresh_interval_seconds": settings.market_data.intraday_refresh_interval_seconds,
        "defaults": {
            "initial_capital": settings.backtest.initial_capital,
            "short_window": settings.backtest.short_window,
            "long_window": settings.backtest.long_window,
            "commission_rate": settings.backtest.commission_rate,
            "minimum_commission": settings.backtest.minimum_commission,
            "stamp_duty_rate": settings.backtest.stamp_duty_rate,
            "slippage_rate": settings.backtest.slippage_rate,
        },
        "paper_trading": {
            "symbols": list(settings.paper_trading.symbols),
            "ma_period": settings.paper_trading.ma_period,
            "breakout_period": settings.paper_trading.breakout_period,
            "atr_period": settings.paper_trading.atr_period,
            "atr_multiplier": settings.paper_trading.atr_multiplier,
        },
        "fund": {
            "exchange_refresh_interval_seconds": settings.fund.exchange_refresh_interval_seconds,
            "open_nav_refresh_interval_seconds": settings.fund.open_nav_refresh_interval_seconds,
            "worker_interval_seconds": settings.fund.worker_interval_seconds,
            "default_initial_capital": settings.fund.default_initial_capital,
            "commission_rate": settings.fund.commission_rate,
            "minimum_commission": settings.fund.minimum_commission,
            "stamp_duty_rate": settings.fund.stamp_duty_rate,
            "slippage_rate": settings.fund.slippage_rate,
            "subscription_fee_rate": settings.fund.subscription_fee_rate,
            "redemption_fee_rate": settings.fund.redemption_fee_rate,
        },
        "stock_screen": {
            "default_universe_size": settings.stock_screen.default_universe_size,
            "max_universe_size": settings.stock_screen.max_universe_size,
            "candidate_count": settings.stock_screen.candidate_count,
        },
    }
