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


class GeneralConfig(BaseModel):
    """通用配置"""
    symbols: list[str] = Field(
        default=["BTC/USDT"],
        description="监控的交易对列表"
    )
    timezone: str = "Asia/Shanghai"
    analysis_interval_minutes: int = Field(
        default=60, ge=5, le=1440,
        description="分析周期（分钟）"
    )


class AppConfig(BaseModel):
    """顶层应用配置，聚合所有子配置"""
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    exchanges: ExchangeConfig = Field(default_factory=ExchangeConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    # 标记是否完成过首次引导
    setup_completed: bool = False
