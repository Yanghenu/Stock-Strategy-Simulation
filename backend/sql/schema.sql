/* 应用数据结构；运行前需由用户创建并配置目标数据库。 */
IF OBJECT_ID(N'dbo.stocks', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.stocks (
        symbol VARCHAR(6) NOT NULL PRIMARY KEY,
        name NVARCHAR(80) NOT NULL,
        exchange VARCHAR(8) NULL,
        updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END;

-- 关注列表独立于回测和纸面实验账本，跨浏览器保存用户选择的标的。
IF OBJECT_ID(N'dbo.market_watchlist', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.market_watchlist (
        symbol VARCHAR(6) NOT NULL PRIMARY KEY,
        name NVARCHAR(80) NOT NULL,
        added_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END;

IF OBJECT_ID(N'dbo.daily_bars', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.daily_bars (
        symbol VARCHAR(6) NOT NULL,
        trade_date DATE NOT NULL,
        open_price DECIMAL(19,6) NOT NULL,
        high_price DECIMAL(19,6) NOT NULL,
        low_price DECIMAL(19,6) NOT NULL,
        close_price DECIMAL(19,6) NOT NULL,
        adj_open DECIMAL(19,6) NOT NULL,
        adj_high DECIMAL(19,6) NOT NULL,
        adj_low DECIMAL(19,6) NOT NULL,
        adj_close DECIMAL(19,6) NOT NULL,
        volume BIGINT NOT NULL DEFAULT 0,
        turnover DECIMAL(24,4) NOT NULL DEFAULT 0,
        CONSTRAINT PK_daily_bars PRIMARY KEY (symbol, trade_date),
        CONSTRAINT FK_daily_bars_stocks FOREIGN KEY (symbol) REFERENCES dbo.stocks(symbol)
    );
END;

IF OBJECT_ID(N'dbo.daily_bar_coverage', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.daily_bar_coverage (
        coverage_id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        symbol VARCHAR(6) NOT NULL,
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        fetched_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_daily_bar_coverage_stocks FOREIGN KEY (symbol) REFERENCES dbo.stocks(symbol),
        CONSTRAINT UQ_daily_bar_coverage UNIQUE (symbol, start_date, end_date)
    );
END;

IF OBJECT_ID(N'dbo.corporate_actions', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.corporate_actions (
        action_id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        symbol VARCHAR(6) NOT NULL,
        source_key VARCHAR(128) NOT NULL,
        record_date DATE NULL,
        ex_date DATE NULL,
        cash_per_share DECIMAL(19,8) NOT NULL DEFAULT 0,
        bonus_shares_per_share DECIMAL(19,8) NOT NULL DEFAULT 0,
        action_status NVARCHAR(40) NOT NULL,
        CONSTRAINT FK_corporate_actions_stocks FOREIGN KEY (symbol) REFERENCES dbo.stocks(symbol),
        CONSTRAINT UQ_corporate_actions_source UNIQUE (symbol, source_key)
    );
END;

IF OBJECT_ID(N'dbo.backtest_runs', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.backtest_runs (
        run_id UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        symbol VARCHAR(6) NOT NULL,
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        strategy_code VARCHAR(32) NOT NULL,
        short_window INT NOT NULL,
        long_window INT NOT NULL,
        ma_period INT NOT NULL CONSTRAINT DF_backtest_runs_ma_period DEFAULT 60,
        breakout_period INT NOT NULL CONSTRAINT DF_backtest_runs_breakout_period DEFAULT 20,
        atr_period INT NOT NULL CONSTRAINT DF_backtest_runs_atr_period DEFAULT 20,
        atr_multiplier DECIMAL(9,4) NOT NULL CONSTRAINT DF_backtest_runs_atr_multiplier DEFAULT 2,
        momentum_period_days INT NOT NULL CONSTRAINT DF_backtest_runs_momentum_period_days DEFAULT 20,
        trend_ma_period INT NOT NULL CONSTRAINT DF_backtest_runs_trend_ma_period DEFAULT 60,
        max_positions INT NOT NULL CONSTRAINT DF_backtest_runs_max_positions DEFAULT 3,
        rebalance_interval_days INT NOT NULL CONSTRAINT DF_backtest_runs_rebalance_interval_days DEFAULT 7,
        initial_capital DECIMAL(19,4) NOT NULL,
        position_ratio DECIMAL(9,6) NOT NULL,
        commission_rate DECIMAL(12,8) NOT NULL,
        minimum_commission DECIMAL(19,4) NOT NULL,
        stamp_duty_rate DECIMAL(12,8) NOT NULL,
        slippage_rate DECIMAL(12,8) NOT NULL,
        status VARCHAR(20) NOT NULL,
        progress INT NOT NULL DEFAULT 0,
        cancel_requested BIT NOT NULL DEFAULT 0,
        total_return DECIMAL(19,8) NULL,
        annualized_return DECIMAL(19,8) NULL,
        max_drawdown DECIMAL(19,8) NULL,
        annualized_volatility DECIMAL(19,8) NULL,
        sharpe_ratio DECIMAL(19,8) NULL,
        win_rate DECIMAL(19,8) NULL,
        closed_trade_count INT NULL,
        fees_total DECIMAL(19,4) NULL,
        dividends_total DECIMAL(19,4) NULL,
        benchmark_return DECIMAL(19,8) NULL,
        error_message NVARCHAR(500) NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        -- 防御型策略独立参数及日期状态，旧记录使用兼容默认值。
        max_holding_days INT NOT NULL CONSTRAINT DF_backtest_runs_max_holding_days DEFAULT 5,
        cooldown_days INT NOT NULL CONSTRAINT DF_backtest_runs_cooldown_days DEFAULT 3,
        target_annual_volatility DECIMAL(19,8) NOT NULL CONSTRAINT DF_backtest_runs_target_annual_volatility DEFAULT 0.15,
        CONSTRAINT FK_backtest_runs_stocks FOREIGN KEY (symbol) REFERENCES dbo.stocks(symbol)
    );
END;

-- 仅补充缺失列，可重复执行；不改变已持久化的任务参数和状态。
IF COL_LENGTH(N'dbo.backtest_runs', N'max_holding_days') IS NULL
    ALTER TABLE dbo.backtest_runs ADD max_holding_days INT NOT NULL CONSTRAINT DF_backtest_runs_max_holding_days DEFAULT 5 WITH VALUES;
IF COL_LENGTH(N'dbo.backtest_runs', N'cooldown_days') IS NULL
    ALTER TABLE dbo.backtest_runs ADD cooldown_days INT NOT NULL CONSTRAINT DF_backtest_runs_cooldown_days DEFAULT 3 WITH VALUES;
IF COL_LENGTH(N'dbo.backtest_runs', N'target_annual_volatility') IS NULL
    ALTER TABLE dbo.backtest_runs ADD target_annual_volatility DECIMAL(19,8) NOT NULL CONSTRAINT DF_backtest_runs_target_annual_volatility DEFAULT 0.15 WITH VALUES;

/* 旧回测数据按原参数保留；新增策略参数只在回测表追加独立数值列。 */
IF COL_LENGTH(N'dbo.backtest_runs', N'ma_period') IS NULL
    ALTER TABLE dbo.backtest_runs ADD ma_period INT NOT NULL CONSTRAINT DF_backtest_runs_ma_period DEFAULT 60 WITH VALUES;
IF COL_LENGTH(N'dbo.backtest_runs', N'breakout_period') IS NULL
    ALTER TABLE dbo.backtest_runs ADD breakout_period INT NOT NULL CONSTRAINT DF_backtest_runs_breakout_period DEFAULT 20 WITH VALUES;
IF COL_LENGTH(N'dbo.backtest_runs', N'atr_period') IS NULL
    ALTER TABLE dbo.backtest_runs ADD atr_period INT NOT NULL CONSTRAINT DF_backtest_runs_atr_period DEFAULT 20 WITH VALUES;
IF COL_LENGTH(N'dbo.backtest_runs', N'atr_multiplier') IS NULL
    ALTER TABLE dbo.backtest_runs ADD atr_multiplier DECIMAL(9,4) NOT NULL CONSTRAINT DF_backtest_runs_atr_multiplier DEFAULT 2 WITH VALUES;
IF COL_LENGTH(N'dbo.backtest_runs', N'momentum_period_days') IS NULL
    ALTER TABLE dbo.backtest_runs ADD momentum_period_days INT NOT NULL CONSTRAINT DF_backtest_runs_momentum_period_days DEFAULT 20 WITH VALUES;
IF COL_LENGTH(N'dbo.backtest_runs', N'trend_ma_period') IS NULL
    ALTER TABLE dbo.backtest_runs ADD trend_ma_period INT NOT NULL CONSTRAINT DF_backtest_runs_trend_ma_period DEFAULT 60 WITH VALUES;
IF COL_LENGTH(N'dbo.backtest_runs', N'max_positions') IS NULL
    ALTER TABLE dbo.backtest_runs ADD max_positions INT NOT NULL CONSTRAINT DF_backtest_runs_max_positions DEFAULT 3 WITH VALUES;
IF COL_LENGTH(N'dbo.backtest_runs', N'rebalance_interval_days') IS NULL
    ALTER TABLE dbo.backtest_runs ADD rebalance_interval_days INT NOT NULL CONSTRAINT DF_backtest_runs_rebalance_interval_days DEFAULT 7 WITH VALUES;

IF OBJECT_ID(N'dbo.backtest_run_symbols', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.backtest_run_symbols (
        run_id UNIQUEIDENTIFIER NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        ordinal INT NOT NULL,
        CONSTRAINT PK_backtest_run_symbols PRIMARY KEY (run_id, symbol),
        CONSTRAINT UQ_backtest_run_symbols_ordinal UNIQUE (run_id, ordinal),
        CONSTRAINT FK_backtest_run_symbols_runs FOREIGN KEY (run_id) REFERENCES dbo.backtest_runs(run_id),
        CONSTRAINT FK_backtest_run_symbols_stocks FOREIGN KEY (symbol) REFERENCES dbo.stocks(symbol)
    );
END;

IF OBJECT_ID(N'dbo.backtest_equity', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.backtest_equity (
        run_id UNIQUEIDENTIFIER NOT NULL,
        trade_date DATE NOT NULL,
        cash DECIMAL(19,4) NOT NULL,
        shares BIGINT NOT NULL,
        market_value DECIMAL(19,4) NOT NULL,
        equity DECIMAL(19,4) NOT NULL,
        drawdown DECIMAL(19,8) NOT NULL,
        benchmark_equity DECIMAL(19,4) NOT NULL,
        CONSTRAINT PK_backtest_equity PRIMARY KEY (run_id, trade_date),
        CONSTRAINT FK_backtest_equity_runs FOREIGN KEY (run_id) REFERENCES dbo.backtest_runs(run_id)
    );
END;

IF OBJECT_ID(N'dbo.backtest_trades', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.backtest_trades (
        trade_id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        run_id UNIQUEIDENTIFIER NOT NULL,
        trade_date DATE NOT NULL,
        signal_date DATE NOT NULL,
        side VARCHAR(8) NOT NULL,
        symbol VARCHAR(6) NULL,
        execution_price DECIMAL(19,6) NOT NULL,
        quantity BIGINT NOT NULL,
        notional DECIMAL(19,4) NOT NULL,
        commission DECIMAL(19,4) NOT NULL,
        stamp_duty DECIMAL(19,4) NOT NULL,
        reason NVARCHAR(80) NOT NULL,
        CONSTRAINT FK_backtest_trades_runs FOREIGN KEY (run_id) REFERENCES dbo.backtest_runs(run_id)
    );
END;

IF COL_LENGTH(N'dbo.backtest_trades', N'symbol') IS NULL
    ALTER TABLE dbo.backtest_trades ADD symbol VARCHAR(6) NULL;

IF OBJECT_ID(N'dbo.backtest_warnings', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.backtest_warnings (
        run_id UNIQUEIDENTIFIER NOT NULL,
        ordinal INT NOT NULL,
        message NVARCHAR(500) NOT NULL,
        CONSTRAINT PK_backtest_warnings PRIMARY KEY (run_id, ordinal),
        CONSTRAINT FK_backtest_warnings_runs FOREIGN KEY (run_id) REFERENCES dbo.backtest_runs(run_id)
    );
END;

IF OBJECT_ID(N'dbo.paper_sessions', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.paper_sessions (
        session_id UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        task_name NVARCHAR(100) NOT NULL CONSTRAINT DF_paper_sessions_task_name DEFAULT N'一个月纸面实验',
        strategy_code VARCHAR(40) NOT NULL,
        status VARCHAR(20) NOT NULL,
        started_at DATETIME2 NOT NULL,
        ends_at DATETIME2 NULL,
        completed_at DATETIME2 NULL,
        initial_capital DECIMAL(19,4) NOT NULL,
        cash DECIMAL(19,4) NOT NULL,
        commission_rate DECIMAL(12,8) NOT NULL,
        minimum_commission DECIMAL(19,4) NOT NULL,
        stamp_duty_rate DECIMAL(12,8) NOT NULL,
        slippage_rate DECIMAL(12,8) NOT NULL,
        ma_period INT NOT NULL,
        breakout_period INT NOT NULL,
        atr_period INT NOT NULL,
        atr_multiplier DECIMAL(9,4) NOT NULL,
        short_ma_period INT NOT NULL CONSTRAINT DF_paper_sessions_short_ma_period DEFAULT 5,
        long_ma_period INT NOT NULL CONSTRAINT DF_paper_sessions_long_ma_period DEFAULT 20,
        momentum_period_days INT NOT NULL CONSTRAINT DF_paper_sessions_momentum_period_days DEFAULT 20,
        trend_ma_period INT NOT NULL CONSTRAINT DF_paper_sessions_trend_ma_period DEFAULT 60,
        max_positions INT NOT NULL CONSTRAINT DF_paper_sessions_max_positions DEFAULT 3,
        rebalance_interval_days INT NOT NULL CONSTRAINT DF_paper_sessions_rebalance_interval_days DEFAULT 7,
        last_rebalanced_at DATETIME2 NULL,
        error_message NVARCHAR(500) NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        -- 防御型策略独立参数及日期状态，旧记录使用兼容默认值。
        max_holding_days INT NOT NULL CONSTRAINT DF_paper_sessions_max_holding_days DEFAULT 5,
        cooldown_days INT NOT NULL CONSTRAINT DF_paper_sessions_cooldown_days DEFAULT 3,
        target_annual_volatility DECIMAL(19,8) NOT NULL CONSTRAINT DF_paper_sessions_target_annual_volatility DEFAULT 0.15,
        max_position_ratio DECIMAL(19,8) NOT NULL CONSTRAINT DF_paper_sessions_max_position_ratio DEFAULT 0.7,
        CONSTRAINT CK_paper_sessions_status CHECK (status IN ('running', 'completed', 'failed', 'stopped'))
    );
END;

-- 仅补充缺失列，可重复执行；不改变已持久化的任务参数和状态。
IF COL_LENGTH(N'dbo.paper_sessions', N'max_holding_days') IS NULL
    ALTER TABLE dbo.paper_sessions ADD max_holding_days INT NOT NULL CONSTRAINT DF_paper_sessions_max_holding_days DEFAULT 5 WITH VALUES;
IF COL_LENGTH(N'dbo.paper_sessions', N'cooldown_days') IS NULL
    ALTER TABLE dbo.paper_sessions ADD cooldown_days INT NOT NULL CONSTRAINT DF_paper_sessions_cooldown_days DEFAULT 3 WITH VALUES;
IF COL_LENGTH(N'dbo.paper_sessions', N'target_annual_volatility') IS NULL
    ALTER TABLE dbo.paper_sessions ADD target_annual_volatility DECIMAL(19,8) NOT NULL CONSTRAINT DF_paper_sessions_target_annual_volatility DEFAULT 0.15 WITH VALUES;
IF COL_LENGTH(N'dbo.paper_sessions', N'max_position_ratio') IS NULL
    ALTER TABLE dbo.paper_sessions ADD max_position_ratio DECIMAL(19,8) NOT NULL CONSTRAINT DF_paper_sessions_max_position_ratio DEFAULT 0.7 WITH VALUES;

-- 为已有月度实验添加任务字段；默认值回填存量行，不触碰其资金、持仓或账本。
IF COL_LENGTH(N'dbo.paper_sessions', N'task_name') IS NULL
    ALTER TABLE dbo.paper_sessions ADD task_name NVARCHAR(100) NOT NULL
        CONSTRAINT DF_paper_sessions_task_name DEFAULT N'一个月纸面实验' WITH VALUES;
IF COL_LENGTH(N'dbo.paper_sessions', N'short_ma_period') IS NULL
    ALTER TABLE dbo.paper_sessions ADD short_ma_period INT NOT NULL
        CONSTRAINT DF_paper_sessions_short_ma_period DEFAULT 5 WITH VALUES;
IF COL_LENGTH(N'dbo.paper_sessions', N'long_ma_period') IS NULL
    ALTER TABLE dbo.paper_sessions ADD long_ma_period INT NOT NULL
        CONSTRAINT DF_paper_sessions_long_ma_period DEFAULT 20 WITH VALUES;
IF COL_LENGTH(N'dbo.paper_sessions', N'momentum_period_days') IS NULL
    ALTER TABLE dbo.paper_sessions ADD momentum_period_days INT NOT NULL
        CONSTRAINT DF_paper_sessions_momentum_period_days DEFAULT 20 WITH VALUES;
IF COL_LENGTH(N'dbo.paper_sessions', N'trend_ma_period') IS NULL
    ALTER TABLE dbo.paper_sessions ADD trend_ma_period INT NOT NULL
        CONSTRAINT DF_paper_sessions_trend_ma_period DEFAULT 60 WITH VALUES;
IF COL_LENGTH(N'dbo.paper_sessions', N'max_positions') IS NULL
    ALTER TABLE dbo.paper_sessions ADD max_positions INT NOT NULL
        CONSTRAINT DF_paper_sessions_max_positions DEFAULT 3 WITH VALUES;
IF COL_LENGTH(N'dbo.paper_sessions', N'rebalance_interval_days') IS NULL
    ALTER TABLE dbo.paper_sessions ADD rebalance_interval_days INT NOT NULL
        CONSTRAINT DF_paper_sessions_rebalance_interval_days DEFAULT 7 WITH VALUES;
IF COL_LENGTH(N'dbo.paper_sessions', N'last_rebalanced_at') IS NULL
    ALTER TABLE dbo.paper_sessions ADD last_rebalanced_at DATETIME2 NULL;
IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id=OBJECT_ID(N'dbo.paper_sessions') AND name=N'ends_at' AND is_nullable=0
)
    ALTER TABLE dbo.paper_sessions ALTER COLUMN ends_at DATETIME2 NULL;

-- 旧版约束只允许一个会话运行；扩充状态枚举以保留停止任务的账本。
IF NOT EXISTS (
    SELECT 1 FROM sys.check_constraints
    WHERE parent_object_id=OBJECT_ID(N'dbo.paper_sessions')
      AND name=N'CK_paper_sessions_status' AND definition LIKE N'%stopped%'
)
BEGIN
    IF EXISTS (
        SELECT 1 FROM sys.check_constraints
        WHERE parent_object_id=OBJECT_ID(N'dbo.paper_sessions') AND name=N'CK_paper_sessions_status'
    )
        ALTER TABLE dbo.paper_sessions DROP CONSTRAINT CK_paper_sessions_status;
    ALTER TABLE dbo.paper_sessions ADD CONSTRAINT CK_paper_sessions_status
        CHECK (status IN ('running', 'completed', 'failed', 'stopped'));
END;

IF OBJECT_ID(N'dbo.paper_session_symbols', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.paper_session_symbols (
        session_id UNIQUEIDENTIFIER NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        budget DECIMAL(19,4) NOT NULL,
        shares BIGINT NOT NULL DEFAULT 0,
        entry_date DATE NULL,
        entry_price DECIMAL(19,6) NULL,
        cost_basis DECIMAL(19,4) NOT NULL DEFAULT 0,
        highest_price DECIMAL(19,6) NULL,
        atr_value DECIMAL(19,6) NULL,
        last_price DECIMAL(19,6) NULL,
        last_quote_at DATETIME2 NULL,
        -- 防御型策略独立参数及日期状态，旧记录使用兼容默认值。
        last_exit_date DATE NULL,
        last_decision_date DATE NULL,
        CONSTRAINT PK_paper_session_symbols PRIMARY KEY (session_id, symbol),
        CONSTRAINT FK_paper_session_symbols_sessions FOREIGN KEY (session_id) REFERENCES dbo.paper_sessions(session_id),
        CONSTRAINT FK_paper_session_symbols_stocks FOREIGN KEY (symbol) REFERENCES dbo.stocks(symbol)
    );
END;

-- 仅补充缺失列，可重复执行；不改变已持久化的任务参数和状态。
IF COL_LENGTH(N'dbo.paper_session_symbols', N'last_exit_date') IS NULL
    ALTER TABLE dbo.paper_session_symbols ADD last_exit_date DATE NULL;
IF COL_LENGTH(N'dbo.paper_session_symbols', N'last_decision_date') IS NULL
    ALTER TABLE dbo.paper_session_symbols ADD last_decision_date DATE NULL;

IF OBJECT_ID(N'dbo.paper_trades', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.paper_trades (
        trade_id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        session_id UNIQUEIDENTIFIER NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        quote_fetched_at DATETIME2 NOT NULL,
        side VARCHAR(8) NOT NULL,
        execution_price DECIMAL(19,6) NOT NULL,
        quantity BIGINT NOT NULL,
        notional DECIMAL(19,4) NOT NULL,
        commission DECIMAL(19,4) NOT NULL,
        stamp_duty DECIMAL(19,4) NOT NULL,
        reason NVARCHAR(100) NOT NULL,
        -- 防御型策略独立参数及日期状态，旧记录使用兼容默认值。
        signal_date DATE NULL,
        CONSTRAINT UQ_paper_trades_quote UNIQUE (session_id, symbol, quote_fetched_at, side),
        CONSTRAINT FK_paper_trades_symbols FOREIGN KEY (session_id, symbol)
            REFERENCES dbo.paper_session_symbols(session_id, symbol)
    );
END;

-- 仅补充缺失列，可重复执行；不改变已持久化的任务参数和状态。
IF COL_LENGTH(N'dbo.paper_trades', N'signal_date') IS NULL
    ALTER TABLE dbo.paper_trades ADD signal_date DATE NULL;

IF OBJECT_ID(N'dbo.paper_equity', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.paper_equity (
        session_id UNIQUEIDENTIFIER NOT NULL,
        trade_date DATE NOT NULL,
        cash DECIMAL(19,4) NOT NULL,
        market_value DECIMAL(19,4) NOT NULL,
        equity DECIMAL(19,4) NOT NULL,
        drawdown DECIMAL(19,8) NOT NULL,
        CONSTRAINT PK_paper_equity PRIMARY KEY (session_id, trade_date),
        CONSTRAINT FK_paper_equity_sessions FOREIGN KEY (session_id) REFERENCES dbo.paper_sessions(session_id)
    );
END;

-- 基金行情和基金纸面实验使用独立表，避免触碰股票实验账本。
IF OBJECT_ID(N'dbo.fund_instruments', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fund_instruments (
        fund_type VARCHAR(12) NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        name NVARCHAR(100) NOT NULL,
        subscription_status NVARCHAR(40) NOT NULL DEFAULT N'未知',
        redemption_status NVARCHAR(40) NOT NULL DEFAULT N'未知',
        fee_description NVARCHAR(100) NOT NULL DEFAULT N'',
        category NVARCHAR(60) NOT NULL DEFAULT N'',
        updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_fund_instruments PRIMARY KEY (fund_type, symbol),
        CONSTRAINT CK_fund_instruments_type CHECK (fund_type IN ('etf', 'lof', 'open_end'))
    );
END;

IF OBJECT_ID(N'dbo.fund_daily_bars', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fund_daily_bars (
        fund_type VARCHAR(12) NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        trade_date DATE NOT NULL,
        open_price DECIMAL(19,6) NOT NULL,
        high_price DECIMAL(19,6) NOT NULL,
        low_price DECIMAL(19,6) NOT NULL,
        close_price DECIMAL(19,6) NOT NULL,
        volume DECIMAL(24,4) NOT NULL DEFAULT 0,
        turnover DECIMAL(24,4) NOT NULL DEFAULT 0,
        CONSTRAINT PK_fund_daily_bars PRIMARY KEY (fund_type, symbol, trade_date),
        CONSTRAINT FK_fund_daily_bars_instrument FOREIGN KEY (fund_type, symbol)
            REFERENCES dbo.fund_instruments(fund_type, symbol)
    );
END;

IF OBJECT_ID(N'dbo.fund_nav_history', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fund_nav_history (
        fund_type VARCHAR(12) NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        trade_date DATE NOT NULL,
        unit_nav DECIMAL(19,8) NOT NULL,
        accumulated_nav DECIMAL(19,8) NULL,
        daily_return DECIMAL(12,8) NULL,
        CONSTRAINT PK_fund_nav_history PRIMARY KEY (fund_type, symbol, trade_date),
        CONSTRAINT FK_fund_nav_history_instrument FOREIGN KEY (fund_type, symbol)
            REFERENCES dbo.fund_instruments(fund_type, symbol)
    );
END;

IF OBJECT_ID(N'dbo.fund_distributions', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fund_distributions (
        fund_type VARCHAR(12) NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        ex_date DATE NOT NULL,
        cash_per_unit DECIMAL(19,8) NOT NULL,
        source_key NVARCHAR(160) NOT NULL,
        CONSTRAINT PK_fund_distributions PRIMARY KEY (fund_type, symbol, ex_date, source_key),
        CONSTRAINT FK_fund_distributions_instrument FOREIGN KEY (fund_type, symbol)
            REFERENCES dbo.fund_instruments(fund_type, symbol)
    );
END;

IF OBJECT_ID(N'dbo.fund_paper_sessions', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fund_paper_sessions (
        session_id UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        task_name NVARCHAR(100) NOT NULL,
        fund_type VARCHAR(12) NOT NULL,
        strategy_code VARCHAR(40) NOT NULL,
        status VARCHAR(12) NOT NULL,
        started_at DATETIME2 NOT NULL,
        completed_at DATETIME2 NULL,
        initial_capital DECIMAL(19,4) NOT NULL,
        cash DECIMAL(19,4) NOT NULL,
        market_value DECIMAL(19,4) NOT NULL DEFAULT 0,
        equity DECIMAL(19,4) NOT NULL,
        total_return DECIMAL(12,8) NOT NULL DEFAULT 0,
        max_drawdown DECIMAL(12,8) NOT NULL DEFAULT 0,
        peak_equity DECIMAL(19,4) NOT NULL,
        commission_rate DECIMAL(12,8) NOT NULL,
        minimum_commission DECIMAL(19,4) NOT NULL,
        stamp_duty_rate DECIMAL(12,8) NOT NULL,
        slippage_rate DECIMAL(12,8) NOT NULL,
        subscription_fee_rate DECIMAL(12,8) NOT NULL,
        redemption_fee_rate DECIMAL(12,8) NOT NULL,
        ma_period INT NOT NULL,
        breakout_period INT NOT NULL,
        atr_period INT NOT NULL,
        atr_multiplier DECIMAL(9,4) NOT NULL,
        short_ma_period INT NOT NULL,
        long_ma_period INT NOT NULL,
        momentum_period_days INT NOT NULL,
        trend_ma_period INT NOT NULL,
        max_positions INT NOT NULL,
        rebalance_interval_days INT NOT NULL,
        risk_lookback_days INT NOT NULL CONSTRAINT DF_fund_paper_sessions_risk_lookback DEFAULT (60),
        last_rebalanced_at DATETIME2 NULL,
        last_processed_at DATETIME2 NULL,
        error_message NVARCHAR(500) NULL,
        CONSTRAINT CK_fund_paper_sessions_type CHECK (fund_type IN ('etf', 'lof', 'open_end')),
        CONSTRAINT CK_fund_paper_sessions_status CHECK (status IN ('running', 'completed', 'failed', 'stopped'))
    );
END;

-- 支持已初始化基金实验表平滑增加调仓时间，避免重启后重置调仓周期。
IF COL_LENGTH(N'dbo.fund_paper_sessions', N'last_rebalanced_at') IS NULL
    ALTER TABLE dbo.fund_paper_sessions ADD last_rebalanced_at DATETIME2 NULL;

-- 为已运行数据库添加低波动策略参数；默认值使现有任务保持兼容。
IF COL_LENGTH(N'dbo.fund_paper_sessions', N'risk_lookback_days') IS NULL
    ALTER TABLE dbo.fund_paper_sessions ADD risk_lookback_days INT NOT NULL
        CONSTRAINT DF_fund_paper_sessions_risk_lookback DEFAULT (60);

IF OBJECT_ID(N'dbo.fund_paper_session_funds', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fund_paper_session_funds (
        session_id UNIQUEIDENTIFIER NOT NULL,
        fund_type VARCHAR(12) NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        name NVARCHAR(100) NOT NULL,
        shares DECIMAL(24,8) NOT NULL DEFAULT 0,
        entry_date DATE NULL,
        entry_price DECIMAL(19,8) NULL,
        highest_price DECIMAL(19,8) NULL,
        entry_atr DECIMAL(19,8) NULL,
        cost_basis DECIMAL(19,4) NOT NULL DEFAULT 0,
        last_price DECIMAL(19,8) NULL,
        last_quote_at DATETIME2 NULL,
        CONSTRAINT PK_fund_paper_session_funds PRIMARY KEY (session_id, fund_type, symbol),
        CONSTRAINT FK_fund_paper_session_funds_session FOREIGN KEY (session_id) REFERENCES dbo.fund_paper_sessions(session_id),
        CONSTRAINT FK_fund_paper_session_funds_instrument FOREIGN KEY (fund_type, symbol)
            REFERENCES dbo.fund_instruments(fund_type, symbol)
    );
END;

-- 保存 ATR 入场参考值和持仓后的最高价，使 worker 重启后能继续跟踪止损。
IF COL_LENGTH(N'dbo.fund_paper_session_funds', N'highest_price') IS NULL
    ALTER TABLE dbo.fund_paper_session_funds ADD highest_price DECIMAL(19,8) NULL;
IF COL_LENGTH(N'dbo.fund_paper_session_funds', N'entry_atr') IS NULL
    ALTER TABLE dbo.fund_paper_session_funds ADD entry_atr DECIMAL(19,8) NULL;

IF OBJECT_ID(N'dbo.fund_paper_orders', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fund_paper_orders (
        order_id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        session_id UNIQUEIDENTIFIER NOT NULL,
        fund_type VARCHAR(12) NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        side VARCHAR(8) NOT NULL,
        order_mode VARCHAR(16) NOT NULL,
        status VARCHAR(12) NOT NULL,
        idempotency_key NVARCHAR(180) NOT NULL UNIQUE,
        signal_date DATE NOT NULL,
        execution_date DATE NULL,
        amount DECIMAL(19,4) NOT NULL DEFAULT 0,
        quantity DECIMAL(24,8) NOT NULL DEFAULT 0,
        execution_price DECIMAL(19,8) NULL,
        fee DECIMAL(19,4) NOT NULL DEFAULT 0,
        reason NVARCHAR(120) NOT NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_fund_paper_orders_status CHECK (status IN ('pending', 'filled', 'cancelled')),
        CONSTRAINT FK_fund_paper_orders_fund FOREIGN KEY (session_id, fund_type, symbol)
            REFERENCES dbo.fund_paper_session_funds(session_id, fund_type, symbol)
    );
END;

IF OBJECT_ID(N'dbo.fund_paper_trades', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fund_paper_trades (
        trade_id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        order_id BIGINT NOT NULL UNIQUE,
        session_id UNIQUEIDENTIFIER NOT NULL,
        fund_type VARCHAR(12) NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        trade_date DATE NOT NULL,
        signal_date DATE NOT NULL,
        side VARCHAR(8) NOT NULL,
        execution_price DECIMAL(19,8) NOT NULL,
        quantity DECIMAL(24,8) NOT NULL,
        notional DECIMAL(19,4) NOT NULL,
        fee DECIMAL(19,4) NOT NULL,
        reason NVARCHAR(120) NOT NULL,
        CONSTRAINT FK_fund_paper_trades_order FOREIGN KEY (order_id) REFERENCES dbo.fund_paper_orders(order_id),
        CONSTRAINT FK_fund_paper_trades_session FOREIGN KEY (session_id) REFERENCES dbo.fund_paper_sessions(session_id)
    );
END;

IF OBJECT_ID(N'dbo.fund_paper_equity', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fund_paper_equity (
        session_id UNIQUEIDENTIFIER NOT NULL,
        trade_date DATE NOT NULL,
        cash DECIMAL(19,4) NOT NULL,
        market_value DECIMAL(19,4) NOT NULL,
        equity DECIMAL(19,4) NOT NULL,
        drawdown DECIMAL(12,8) NOT NULL,
        CONSTRAINT PK_fund_paper_equity PRIMARY KEY (session_id, trade_date),
        CONSTRAINT FK_fund_paper_equity_session FOREIGN KEY (session_id) REFERENCES dbo.fund_paper_sessions(session_id)
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'IX_backtest_runs_created' AND object_id=OBJECT_ID(N'dbo.backtest_runs'))
    CREATE INDEX IX_backtest_runs_created ON dbo.backtest_runs(created_at DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'IX_backtest_trades_run_date' AND object_id=OBJECT_ID(N'dbo.backtest_trades'))
    CREATE INDEX IX_backtest_trades_run_date ON dbo.backtest_trades(run_id, trade_date);
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'UX_paper_sessions_running' AND object_id=OBJECT_ID(N'dbo.paper_sessions'))
    DROP INDEX UX_paper_sessions_running ON dbo.paper_sessions;
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'IX_paper_sessions_status_started' AND object_id=OBJECT_ID(N'dbo.paper_sessions'))
    CREATE INDEX IX_paper_sessions_status_started ON dbo.paper_sessions(status, started_at DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'IX_paper_trades_session_quote' AND object_id=OBJECT_ID(N'dbo.paper_trades'))
    CREATE INDEX IX_paper_trades_session_quote ON dbo.paper_trades(session_id, quote_fetched_at DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'IX_fund_instruments_name' AND object_id=OBJECT_ID(N'dbo.fund_instruments'))
    CREATE INDEX IX_fund_instruments_name ON dbo.fund_instruments(name, fund_type);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'IX_fund_paper_sessions_status' AND object_id=OBJECT_ID(N'dbo.fund_paper_sessions'))
    CREATE INDEX IX_fund_paper_sessions_status ON dbo.fund_paper_sessions(status, started_at DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'IX_fund_paper_trades_session_date' AND object_id=OBJECT_ID(N'dbo.fund_paper_trades'))
    CREATE INDEX IX_fund_paper_trades_session_date ON dbo.fund_paper_trades(session_id, trade_date DESC);

-- 选股研究账本与既有纸面交易表完全独立；不保存 JSON，也不修改既有任务。
IF OBJECT_ID(N'dbo.stock_screen_runs', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.stock_screen_runs (
        run_id UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        status VARCHAR(12) NOT NULL,
        signal_cutoff DATE NOT NULL,
        requested_count INT NOT NULL,
        candidate_count INT NOT NULL,
        rule_version VARCHAR(20) NOT NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        completed_at DATETIME2 NULL,
        error_message NVARCHAR(500) NULL,
        CONSTRAINT CK_stock_screen_runs_status CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled'))
    );
END;

IF OBJECT_ID(N'dbo.stock_screen_items', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.stock_screen_items (
        run_id UNIQUEIDENTIFIER NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        name NVARCHAR(100) NOT NULL,
        snapshot_turnover DECIMAL(19,2) NOT NULL,
        status VARCHAR(12) NOT NULL DEFAULT 'pending',
        signal_date DATE NULL,
        signal_close DECIMAL(19,6) NULL,
        momentum_score DECIMAL(16,8) NULL,
        recent_5_return DECIMAL(16,8) NULL,
        reversal_score DECIMAL(16,8) NULL,
        momentum_rank INT NULL,
        reversal_rank INT NULL,
        last_evaluated_date DATE NULL,
        error_message NVARCHAR(500) NULL,
        CONSTRAINT PK_stock_screen_items PRIMARY KEY (run_id, symbol),
        CONSTRAINT FK_stock_screen_items_run FOREIGN KEY (run_id) REFERENCES dbo.stock_screen_runs(run_id),
        CONSTRAINT CK_stock_screen_items_status CHECK (status IN ('pending', 'eligible', 'excluded', 'error'))
    );
END;

IF OBJECT_ID(N'dbo.stock_screen_outcomes', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.stock_screen_outcomes (
        run_id UNIQUEIDENTIFIER NOT NULL,
        symbol VARCHAR(6) NOT NULL,
        horizon_days INT NOT NULL,
        outcome_date DATE NOT NULL,
        outcome_close DECIMAL(19,6) NOT NULL,
        return_rate DECIMAL(16,8) NOT NULL,
        CONSTRAINT PK_stock_screen_outcomes PRIMARY KEY (run_id, symbol, horizon_days),
        CONSTRAINT FK_stock_screen_outcomes_item FOREIGN KEY (run_id, symbol)
            REFERENCES dbo.stock_screen_items(run_id, symbol),
        CONSTRAINT CK_stock_screen_outcomes_horizon CHECK (horizon_days IN (5, 20))
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'IX_stock_screen_runs_created' AND object_id=OBJECT_ID(N'dbo.stock_screen_runs'))
    CREATE INDEX IX_stock_screen_runs_created ON dbo.stock_screen_runs(created_at DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'IX_stock_screen_items_status' AND object_id=OBJECT_ID(N'dbo.stock_screen_items'))
    CREATE INDEX IX_stock_screen_items_status ON dbo.stock_screen_items(run_id, status, symbol);
