"""AI 分析报告生成器。

将结构化的评分数据交给 AI 大模型，生成人类可读的分析报告。
AI 的角色是"翻译官"——把数据翻译成通俗分析，而非决策者。

支持 DeepSeek / OpenAI 等兼容 OpenAI API 格式的模型。
"""

from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

from config.schema import AIConfig
from core.interfaces import AIProvider
from core.models import MarketSnapshot

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位专业的加密货币量化分析师，遵循"小亏大赚"交易哲学。
你的任务是根据多维度市场数据和量化交易建议，生成简洁、准确的交易分析报告。

## 核心规则
1. 用中文撰写，语言简洁专业
2. 严格基于提供的数据分析，不要编造数据或臆测
3. 重点突出最重要的 2-3 个信号，解释为什么重要
4. 如果信号矛盾或不明确，明确建议观望，不要强行给出方向
5. 用 ⚠️ 标注风险事件和注意事项
6. 输出控制在 400-600 字以内

## 交易建议规则（小亏大赚原则）
- 当系统已给出交易建议时，对其进行解读和补充，说明入场理由
- 止损必须明确，不允许"看情况再说"
- 盈亏比 < 1.5 的机会必须明确标注为"不建议开仓"
- 分两档止盈目标：保守（部分止盈）和激进（剩余仓位）
- 所有价格必须精确到整数位（BTC）或两位小数（其他币种）"""

ANALYSIS_PROMPT_TEMPLATE = """## 当前市场数据
{snapshot_json}

## 评分引擎结果
{score_summary}

## 量化交易建议
{trade_summary}

## 请生成分析报告
包含以下内容：
1. **总体判断** — 一句话概括当前趋势和信心度
2. **核心信号** — 最重要的 2-3 个数据点及其意义
3. **关键价位** — 支撑和阻力，标注数据来源
4. **交易策略**（基于系统给出的量化建议）
   - 方向和仓位建议
   - 入场区间（回调买入 / 反弹做空）
   - 止损价位和依据
   - 保守止盈（建议平一半仓位）
   - 激进止盈（剩余仓位目标）
   - 盈亏比评估
   - 如果盈亏比不达标，明确说"当前不建议开仓，等待更好位置"
5. **风险提示** — 事件、异常指标、可能导致策略失效的因素"""


class AIReporter(AIProvider):
    """基于 OpenAI 兼容 API 的 AI 分析器"""

    def __init__(self, config: AIConfig):
        self._config = config
        self._client: AsyncOpenAI | None = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled and bool(self._config.api_key)

    def update_config(self, config: AIConfig) -> None:
        """热更新配置，重置客户端以使用新的 API Key / Base URL"""
        self._config = config
        self._client = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._config.api_key,
                base_url=self._config.base_url,
            )
        return self._client

    async def analyze(
        self, snapshot_dict: dict, score_summary: str, trade_summary: str = "",
    ) -> str:
        """调用 AI 生成分析报告"""
        if not self.enabled:
            return "AI 分析未启用（请在配置中填写 API Key）"

        user_prompt = ANALYSIS_PROMPT_TEMPLATE.format(
            snapshot_json=json.dumps(snapshot_dict, ensure_ascii=False, indent=2),
            score_summary=score_summary,
            trade_summary=trade_summary or "暂无（信号中性或关键位不足，不建议开仓）",
        )

        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self._config.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=self._config.max_tokens,
                temperature=self._config.temperature,
            )
            content = response.choices[0].message.content or ""
            logger.info("AI 分析完成，输出 %d 字符", len(content))
            return content.strip()

        except Exception as e:
            logger.error("AI 分析调用失败: %s", e)
            return f"AI 分析暂时不可用: {e}"

    async def test_connection(self) -> bool:
        """测试 AI 服务是否可用"""
        if not self.enabled:
            return False
        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "user", "content": "回复OK"}],
                max_tokens=10,
            )
            return bool(response.choices)
        except Exception as e:
            logger.error("AI 连接测试失败: %s", e)
            return False


def build_score_summary(report) -> str:
    """将评分结果格式化为 AI 可理解的摘要文本"""
    lines = [
        f"总分：{report.total_score:+.0f}/{report.max_possible_score:.0f}",
        f"方向：{report.direction_label}",
        f"信心度：{report.confidence:.0f}%",
        f"信号强度：{report.signal_strength.value}",
        "",
        "各维度评分：",
    ]
    for fs in report.factor_scores:
        emoji = "📈" if fs.score > 0 else ("📉" if fs.score < 0 else "➖")
        lines.append(
            f"  {fs.name}: {fs.score:+.0f}/{fs.max_score:.0f} {emoji} | {fs.details}"
        )

    if report.key_levels.supports:
        lines.append("\n支撑位：")
        for lv in report.key_levels.supports[:3]:
            lines.append(f"  {lv.price:.0f} ({lv.source}, {lv.strength})")

    if report.key_levels.resistances:
        lines.append("阻力位：")
        for lv in report.key_levels.resistances[:3]:
            lines.append(f"  {lv.price:.0f} ({lv.source}, {lv.strength})")

    return "\n".join(lines)


def build_trade_summary(report) -> str:
    """将 TradeSuggestion 格式化为 AI 可消费的摘要文本。

    返回空字符串表示无建议（中性/盈亏比不足等）。
    """
    ts = report.trade_suggestion
    if ts is None:
        return ""

    d_label = {"bullish": "做多", "bearish": "做空"}.get(ts.direction.value, "")
    p_label = {
        "skip": "不建议开仓", "light": "轻仓",
        "normal": "标准仓位", "heavy": "可加仓",
    }.get(ts.position_size.value, "")

    lines = [
        f"方向: {d_label}",
        f"仓位: {p_label}",
        f"入场区间: ${ts.entry_low:.0f} - ${ts.entry_high:.0f}",
        f"止损: ${ts.stop_loss:.0f} (参考: {ts.sl_source})",
        f"保守止盈: ${ts.take_profit_1:.0f} (参考: {ts.tp1_source})",
        f"激进止盈: ${ts.take_profit_2:.0f} (参考: {ts.tp2_source})" if ts.tp2_source else "",
        f"盈亏比: 保守 {ts.risk_reward_1:.1f}:1 / 激进 {ts.risk_reward_2:.1f}:1",
        f"综合理由: {ts.reasoning}",
    ]
    return "\n".join(line for line in lines if line)
