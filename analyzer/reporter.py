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

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位专业的加密货币量化分析师，遵循"小亏大赚"交易哲学。
你的任务是根据多维度市场数据和条件挂单策略，生成简洁、准确的交易分析报告。

## 核心规则
1. 用中文撰写，语言简洁专业
2. 严格基于提供的数据分析，不要编造数据或臆测
3. 重点突出最重要的 2-3 个信号，解释为什么重要
4. 如果信号矛盾或不明确，明确建议观望，不要强行给出方向
5. 用 ⚠️ 标注风险事件和注意事项
6. 输出控制在 400-600 字以内

## 宏观指标专业解读指引
你会收到以下宏观数据，请按以下逻辑解读：

**VIX 恐慌指数**：
- VIX < 15 → 市场平静，风险偏好强，加密货币通常正常波动
- VIX 15-25 → 正常范围，关注变化方向而非绝对值
- VIX 25-35 → 市场紧张，资金可能从风险资产（含加密）撤出
- VIX > 35 → 极度恐慌，可能是"恐慌卖出后的反弹买入"机会
- 核心：VIX 急升（日涨 >20%）= 风险事件，短线利空加密；VIX 高位回落 = 情绪修复，利多

**美国 10 年期国债收益率**：
- 收益率上升 → 持有无息资产（BTC/黄金）的机会成本增加 → 利空
- 收益率日升 >5bp → 强利空加密，尤其对杠杆仓位
- 收益率下降 → 资金寻求替代投资 → 利多加密
- 核心：关注变化方向和速度，而非绝对水平

**COMEX 黄金**：
- 黄金与 BTC 的关系是"有时同步，有时分化"，取决于驱动因素
- 黄金上涨 + 美元下跌 → 通常利多 BTC（避险+弱美元双击）
- 黄金上涨 + 美元上涨 → 纯避险驱动，BTC 可能反而受压
- 黄金大跌 → 可能是流动性紧缩（所有资产同跌），警惕
- 核心：把黄金涨跌作为"风险情绪温度计"而非直接因果

**恐惧贪婪指数 (Fear & Greed)**：
- 极度恐惧 (<20) 是反向做多参考，但持续恐惧（连续多天 <25）不宜盲目抄底
- 极度贪婪 (>80) 是反向做空参考，但牛市中贪婪可持续数周
- 核心：关注变化率而非绝对值，从 50→20 的急跌比长期维持 20 更有交易价值

## 技术指标解读指引
**ATR（真实波幅）**：
- atr_pct > 3% → 高波动环境，止损应放宽，轻仓操作
- atr_pct 1-3% → 正常波动
- atr_pct < 1% → 低波动（挤压态），可能酝酿大行情突破

**MA20/MA60 金叉死叉**：
- golden_cross（金叉刚形成）→ 中期多头信号
- death_cross（死叉刚形成）→ 中期空头信号
- 已经运行中的金叉/死叉权重递减，关注最新交叉事件

**资金费率趋势**：
- 连续负费率 ≥5 天 → 空头持续付费，杠杆空头拥挤，不易继续下跌
- 连续正费率 ≥3 天 → 多头付费加重，杠杆多头拥挤，回调概率增大

## 交易建议规则（小亏大赚 + 条件挂单）
- 系统始终提供 2-3 个条件策略（回调做多/反弹做空/突破追单），不论方向如何
- 对每个策略进行解读：为什么这个价位值得关注，哪个策略当前最优
- 止损必须明确，不允许"看情况再说"
- 盈亏比 < 1.5 的策略标注为"盈亏比不足，谨慎操作"
- 如果所有策略都不达标，明确说"当前不建议开仓，等待更好位置"
- 说明策略的失效条件（什么情况下该放弃挂单）
- 所有价格精确到整数位（BTC）或两位小数（其他币种）"""

ANALYSIS_PROMPT_TEMPLATE = """## 当前市场数据
{snapshot_json}

## 评分引擎结果
{score_summary}

## 条件挂单策略
{trade_summary}

## 请生成分析报告
包含以下内容：
1. **总体判断** — 一句话概括当前趋势和信心度
2. **核心信号** — 最重要的 2-3 个数据点及其意义
3. **关键价位** — 支撑和阻力，标注数据来源
4. **交易策略**（基于系统给出的条件挂单策略）
   - 当前即时建议（应该观望还是可以操作）
   - 逐一解读每个条件策略：
     - 策略类型和触发价格
     - 为什么这个价位值得关注
     - 入场区间、止损、止盈
     - 盈亏比评估
     - 何时该放弃该策略（失效条件）
   - 综合推荐：当前最优的 1 个策略及理由
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
    state_labels = {
        "strong_trend": "强趋势", "trend_weakening": "趋势衰减",
        "ranging": "震荡", "extreme_divergence": "极端背离",
    }
    ms = getattr(report, "market_state", None)
    ms_label = state_labels.get(ms.value, ms.value) if ms else "未分类"
    trigger = getattr(report, "trigger_reason", "") or "定时分析"

    lines = [
        f"总分：{report.total_score:+.0f}/{report.max_possible_score:.0f}",
        f"方向：{report.direction_label}",
        f"信心度：{report.confidence:.0f}%",
        f"信号强度：{report.signal_strength.value}",
        f"市场状态：{ms_label}",
        f"触发原因：{trigger}",
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
    """将 TradePlan 格式化为 AI 可消费的摘要文本。

    优先使用新版 trade_plan，兼容旧版 trade_suggestion。
    """
    plan = getattr(report, "trade_plan", None)
    if plan and plan.strategies:
        return _format_trade_plan(plan)

    # 旧版兼容
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


_BIAS_CN = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}
_SIZE_CN = {"skip": "待观察", "light": "轻仓", "normal": "标准", "heavy": "重仓"}


def _format_trade_plan(plan) -> str:
    """将 TradePlan 格式化为结构化文本。"""
    bias = _BIAS_CN.get(plan.market_bias.value, "未知")
    lines = [
        f"市场偏向: {bias}",
        f"即时建议: {plan.immediate_action}",
        "",
    ]

    for i, s in enumerate(plan.strategies, 1):
        size_label = _SIZE_CN.get(s.position_size.value, s.position_size.value)
        lines.extend([
            f"--- 策略{i}: {s.label} ---",
            f"  触发/挂单价: ${s.trigger_price:.0f}",
            f"  入场区间: ${s.entry_low:.0f} - ${s.entry_high:.0f}",
            f"  止损: ${s.stop_loss:.0f} ({s.sl_source})",
            f"  保守止盈: ${s.take_profit_1:.0f} ({s.tp1_source})",
            f"  激进止盈: ${s.take_profit_2:.0f}",
            f"  盈亏比: {s.risk_reward:.1f}:1 | 入场质量: {s.entry_quality:.0f}/100 → {size_label}",
            f"  有效期: {s.valid_hours}小时",
            f"  失效条件: {s.invalidation}" if s.invalidation else "",
            f"  理由: {s.reasoning}",
            "",
        ])

    if plan.analysis_note:
        lines.append(f"总体说明: {plan.analysis_note}")

    return "\n".join(line for line in lines if line)
