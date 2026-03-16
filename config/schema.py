"""配置数据验证 Schema。

使用 Pydantic 模型确保配置值合法。
每个子配置对应系统中的一个功能模块。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExchangeConfig(BaseModel):
    """交易所数据源配置"""
    primary: str = Field(default="okx", description="主交易所（用于交易执行）")
    secondary: str = Field(default="binance", description="辅交易所（数据补充）")
    options_source: str = Field(default="deribit", description="期权数据源")


class FactorWeightConfig(BaseModel):
    """单个评分因子的配置"""
    enabled: bool = True
    weight: float = 15.0


class ScoringConfig(BaseModel):
    """评分引擎配置"""
    technical: FactorWeightConfig = FactorWeightConfig(weight=20.0)
    funding_rate: FactorWeightConfig = FactorWeightConfig(weight=15.0)
    open_interest: FactorWeightConfig = FactorWeightConfig(weight=15.0)
    long_short_ratio: FactorWeightConfig = FactorWeightConfig(weight=15.0)
    options: FactorWeightConfig = FactorWeightConfig(weight=20.0)
    macro: FactorWeightConfig = FactorWeightConfig(weight=20.0)
    sentiment: FactorWeightConfig = FactorWeightConfig(weight=15.0)

    def get_factor_config(self, name: str) -> FactorWeightConfig:
        return getattr(self, name, FactorWeightConfig())


class EmailConfig(BaseModel):
    """邮件推送配置"""
    enabled: bool = Field(default=True, description="是否启用邮件")
    smtp_host: str = "smtp.163.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_pass: str = ""
    from_name: str = "CryptoSignal Hub"
    to: list[str] = Field(default_factory=list)
    use_ssl: bool = True


class ScheduleConfig(BaseModel):
    """推送调度配置"""
    # 每小时分析报告邮件，默认关闭
    hourly_report_email: bool = Field(
        default=False,
        description="是否每小时发送分析邮件（默认关闭）"
    )
    # 日报发送时间（24h 格式，基于配置的时区）
    daily_report_times: list[str] = Field(
        default=["09:00", "21:00"],
        description="日报发送时间"
    )
    # 强信号告警：始终开启，捕捉到立即发送，不受时间限制
    strong_signal_alert: bool = Field(
        default=True,
        description="强信号实时推送（不受静默时段限制）"
    )
    # 美股开盘前后特别监控
    us_market_alert: bool = Field(
        default=True,
        description="美股开盘前后加强监控"
    )
    # 静默时段（仅影响普通报告，不影响强信号）
    quiet_hours_start: str = "00:00"
    quiet_hours_end: str = "07:00"
    # 防骚扰
    max_daily_emails: int = Field(default=15, ge=1, le=50)
    duplicate_signal_cooldown_hours: int = Field(default=4, ge=1, le=24)


class AIConfig(BaseModel):
    """AI 分析配置"""
    enabled: bool = True
    provider: str = Field(default="deepseek", description="deepseek / openai")
    api_key: str = ""
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    max_tokens: int = 2000
    temperature: float = 0.3


class SentinelConfig(BaseModel):
    """哨兵监控配置"""
    enabled: bool = Field(default=True, description="是否启用哨兵实时监控")
    price_check_interval: int = Field(
        default=45, ge=10, le=300,
        description="价格检查间隔（秒）"
    )
    derivatives_check_interval: int = Field(
        default=300, ge=60, le=900,
        description="OI/资金费率检查间隔（秒）"
    )
    breakout_buffer_pct: float = Field(
        default=0.2, ge=0.05, le=1.0,
        description="突破判定缓冲（价格超过关键位 X% 才算突破）"
    )
    rapid_move_pct: float = Field(
        default=2.0, ge=0.5, le=10.0,
        description="短时大幅波动阈值（15 分钟内涨跌超 X%）"
    )
    oi_change_threshold_pct: float = Field(
        default=10.0, ge=3.0, le=30.0,
        description="OI 异动阈值（变化超 X%）"
    )
    funding_extreme_rate: float = Field(
        default=0.001, ge=0.0003, le=0.01,
        description="资金费率极端阈值（绝对值）"
    )
    cooldown_minutes: int = Field(
        default=30, ge=5, le=120,
        description="两次事件触发分析的最小间隔（分钟）"
    )
    level_cooldown_minutes: int = Field(
        default=60, ge=15, le=240,
        description="同一关键位重复触发的冷却时间（分钟）"
    )


class ExecutorConfig(BaseModel):
    """执行层配置（独立插件，默认关闭）"""
    enabled: bool = Field(default=False, description="是否启用自动执行")
    mode: str = Field(default="demo", description="demo=模拟盘 / live=实盘")
    exchange: str = Field(default="okx", description="执行交易所")
    api_key: str = Field(default="", description="交易所 API Key")
    api_secret: str = Field(default="", description="交易所 API Secret")
    passphrase: str = Field(default="", description="交易所 Passphrase（OKX 必填）")
    # 风控参数
    max_positions: int = Field(default=2, ge=1, le=10, description="最大同时持仓数")
    max_position_pct: float = Field(
        default=10.0, ge=1.0, le=50.0, description="单笔仓位占总权益百分比上限"
    )
    min_risk_reward: float = Field(
        default=1.5, ge=1.0, le=5.0, description="最低盈亏比门槛"
    )
    daily_loss_limit_pct: float = Field(
        default=5.0, ge=1.0, le=20.0, description="当日亏损占权益百分比上限（触发熔断）"
    )
    default_leverage: int = Field(default=3, ge=1, le=20, description="默认杠杆")
    min_confidence: int = Field(
        default=65, ge=0, le=100, description="最低信号信心度"
    )
    min_signal_strength: str = Field(
        default="moderate", description="最低信号强度: strong / moderate"
    )
    auto_execute: bool = Field(
        default=False, description="自动执行（False=仅记录不下单）"
    )
    # 分级资金分配（基于 PositionSize 档位）
    light_position_pct: float = Field(
        default=4.0, ge=1.0, le=20.0, description="LIGHT 档仓位占权益 %"
    )
    normal_position_pct: float = Field(
        default=7.0, ge=2.0, le=30.0, description="NORMAL 档仓位占权益 %"
    )
    heavy_position_pct: float = Field(
        default=11.0, ge=3.0, le=40.0, description="HEAVY 档仓位占权益 %"
    )
    enable_dynamic_sizing: bool = Field(
        default=True, description="启用动态仓位调节（信心度/盈亏比/市场状态因子）"
    )
    consecutive_loss_shrink: bool = Field(
        default=True, description="连亏自动缩仓"
    )
    enable_trailing_stop: bool = Field(
        default=False, description="启用移动止损（TP1后SL移到盈亏平衡）"
    )
    enable_signal_export: bool = Field(
        default=True, description="自动存档信号和执行记录"
    )
    # 策略预设
    preset: str = Field(
        default="balanced", description="参数预设: conservative/balanced/aggressive/custom"
    )


class GeneralConfig(BaseModel):
    """通用配置"""
    symbols: list[str] = Field(
        default=["BTC/USDT"],
        description="监控的交易对列表"
    )
    timezone: str = "Asia/Shanghai"
    analysis_interval_minutes: int = Field(
        default=240, ge=5, le=1440,
        description="全量分析周期（分钟），哨兵启用时建议 240"
    )
    strategy_mode: str = Field(
        default="adaptive",
        description="策略模式: adaptive(三档自适应) / trend_only(只顺势)"
    )


class AppConfig(BaseModel):
    """顶层应用配置，聚合所有子配置"""
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    exchanges: ExchangeConfig = Field(default_factory=ExchangeConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    sentinel: SentinelConfig = Field(default_factory=SentinelConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    # 标记是否完成过首次引导
    setup_completed: bool = False
