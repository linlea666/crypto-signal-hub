"""抽象接口定义。

定义系统各层之间的契约。新增数据源、评分因子或通知渠道时，
只需实现对应接口，无需修改已有代码（开闭原则）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.models import FactorScore, MarketSnapshot, SignalReport


class DataCollector(ABC):
    """数据采集器接口。

    每个采集器负责从一个特定数据源获取数据，
    并将结果填充到 MarketSnapshot 对应字段中。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """采集器名称，用于日志和状态展示"""

    @abstractmethod
    async def collect(self, symbol: str, snapshot_data: dict) -> dict:
        """采集数据并更新 snapshot_data 字典。

        Args:
            symbol: 交易对标识（如 "BTC/USDT"）
            snapshot_data: 可变字典，各采集器向其中填充自己负责的字段

        Returns:
            更新后的 snapshot_data

        Raises:
            CollectorError: 采集失败时抛出，不应中断其他采集器
        """

    async def initialize(self) -> None:
        """可选的初始化方法，在首次采集前调用"""

    async def cleanup(self) -> None:
        """可选的清理方法，系统关闭时调用"""


class ScoreFactor(ABC):
    """评分因子接口。

    每个因子负责从 MarketSnapshot 中提取特定维度的信息，
    并计算出一个带方向的评分。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """因子名称"""

    @property
    @abstractmethod
    def max_score(self) -> float:
        """该因子的满分值（正值）"""

    @abstractmethod
    def calculate(self, snapshot: MarketSnapshot) -> FactorScore:
        """基于快照数据计算评分。

        Args:
            snapshot: 完整的市场数据快照

        Returns:
            FactorScore: 评分结果，score 范围为 [-max_score, +max_score]
        """


class Notifier(ABC):
    """通知渠道接口。

    支持邮件、Telegram 等多种推送渠道。
    每个实现只负责具体的发送逻辑。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """渠道名称"""

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """当前是否启用"""

    @abstractmethod
    async def send(self, report: SignalReport, html_content: str) -> bool:
        """发送通知。

        Args:
            report: 信号报告对象
            html_content: 预渲染的 HTML 内容

        Returns:
            是否发送成功
        """

    async def send_test(self) -> bool:
        """发送测试消息，验证配置是否正确"""
        return False


class ReportRenderer(ABC):
    """报告渲染器接口，将 SignalReport 转为特定格式"""

    @abstractmethod
    def render(self, report: SignalReport) -> str:
        """渲染报告为目标格式（HTML / 纯文本 / Markdown）"""


class AIProvider(ABC):
    """AI 分析提供者接口"""

    @abstractmethod
    async def analyze(
        self, snapshot_dict: dict, score_summary: str, trade_summary: str = "",
    ) -> str:
        """基于结构化数据生成分析文本。

        Args:
            snapshot_dict: MarketSnapshot 的字典形式
            score_summary: 评分引擎的结构化摘要
            trade_summary: 量化交易建议摘要（可选，为空时 AI 自行判断）

        Returns:
            AI 生成的自然语言分析报告
        """

    async def test_connection(self) -> bool:
        """测试 AI 服务连接是否正常"""
        return False
