"""多因子评分引擎。

编排所有 ScoreFactor，汇总评分，判断方向和信号强度。
因子通过注册机制加入，新增因子不需要修改此文件。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from config.schema import ScoringConfig
from core.constants import (
    CONFIDENCE_MODERATE_THRESHOLD,
    CONFIDENCE_STRONG_THRESHOLD,
    Direction,
    MarketState,
    SignalStrength,
)
from core.interfaces import ScoreFactor
from core.models import (
    FactorScore,
    KeyLevels,
    MarketSnapshot,
    SignalReport,
)
from engine.confidence import calculate_confidence
from engine.levels import identify_key_levels
from engine.market_state import classify_from_snapshot
from engine.trade_advisor import derive_trade_plan, derive_trade_suggestion

logger = logging.getLogger(__name__)


class SignalScorer:
    """多因子评分引擎，系统的核心决策模块。

    职责：
    1. 执行所有已注册因子的评分
    2. 汇总得分并判断方向
    3. 计算信心度
    4. 识别关键价位
    5. 组装 SignalReport
    """

    def __init__(self, scoring_config: ScoringConfig):
        self._config = scoring_config
        self._factors: list[ScoreFactor] = []

    def update_config(self, scoring_config: ScoringConfig) -> None:
        """热重载评分配置（权重/启用状态变更立即生效）"""
        self._config = scoring_config
        logger.info("评分引擎配置已更新")

    def register_factor(self, factor: ScoreFactor) -> None:
        self._factors.append(factor)
        logger.info("注册评分因子: %s (满分 ±%.0f)", factor.name, factor.max_score)

    def unregister_factor(self, name: str) -> bool:
        """按名称移除因子，返回是否成功移除。"""
        before = len(self._factors)
        self._factors = [f for f in self._factors if f.name != name]
        removed = len(self._factors) < before
        if removed:
            logger.info("注销评分因子: %s", name)
        return removed

    def has_factor(self, name: str) -> bool:
        return any(f.name == name for f in self._factors)

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        *,
        strategy_mode: str = "adaptive",
    ) -> SignalReport:
        """执行完整评分流程，生成信号报告。"""
        # 1. 逐因子评分（按配置权重加权）
        factor_scores = self._calculate_all_factors(snapshot)

        # 2. 汇总得分
        total_score = sum(fs.score for fs in factor_scores)
        max_possible = sum(fs.max_score for fs in factor_scores)

        # 3. 判断方向（动态阈值：满分的 8%）
        direction = self._determine_direction(total_score, max_possible)

        # 4. 计算信心度（传入事件列表用于衰减）
        confidence = calculate_confidence(factor_scores, snapshot.events)

        # 5. 判断信号强度
        strength = self._determine_strength(confidence)

        # 6. 识别关键价位
        key_levels = identify_key_levels(snapshot)

        # 7. 市场状态分类
        market_state = classify_from_snapshot(total_score, confidence, snapshot)

        # 8. 推导条件策略计划（受市场状态约束）
        trade_plan = derive_trade_plan(
            direction=direction,
            confidence=confidence,
            price=snapshot.price,
            levels=key_levels,
            market_state=market_state,
            strategy_mode=strategy_mode,
        )

        # 旧版兼容
        trade_suggestion = derive_trade_suggestion(
            direction=direction,
            confidence=confidence,
            price=snapshot.price,
            levels=key_levels,
            plan=trade_plan,
        )

        return SignalReport(
            id=str(uuid.uuid4()),
            timestamp=snapshot.timestamp,
            symbol=snapshot.symbol,
            snapshot=snapshot,
            factor_scores=factor_scores,
            total_score=round(total_score, 1),
            max_possible_score=max_possible,
            direction=direction,
            confidence=round(confidence, 1),
            signal_strength=strength,
            key_levels=key_levels,
            trade_suggestion=trade_suggestion,
            trade_plan=trade_plan,
            market_state=market_state,
        )

    def _calculate_all_factors(
        self, snapshot: MarketSnapshot
    ) -> list[FactorScore]:
        """执行所有因子评分，按配置权重缩放，单个因子异常不影响整体。"""
        results: list[FactorScore] = []
        for factor in self._factors:
            factor_config = self._config.get_factor_config(factor.name)
            if not factor_config.enabled:
                continue
            try:
                raw_score = factor.calculate(snapshot)
                # 按配置权重缩放：config_weight / factor_max_score
                cfg_weight = factor_config.weight
                if raw_score.max_score > 0 and cfg_weight != raw_score.max_score:
                    scale = cfg_weight / raw_score.max_score
                    scaled = FactorScore(
                        name=raw_score.name,
                        score=round(raw_score.score * scale, 1),
                        max_score=cfg_weight,
                        direction=raw_score.direction,
                        details=raw_score.details,
                    )
                    results.append(scaled)
                else:
                    results.append(raw_score)
            except Exception as e:
                logger.error("因子 %s 评分异常: %s", factor.name, e, exc_info=True)
                results.append(FactorScore(
                    name=factor.name,
                    score=0,
                    max_score=factor.max_score,
                    direction=Direction.NEUTRAL,
                    details=f"评分异常: {e}",
                ))
        return results

    @staticmethod
    def _determine_direction(
        total_score: float, max_possible: float = 120.0,
    ) -> Direction:
        threshold = max(8.0, max_possible * 0.08)
        if total_score > threshold:
            return Direction.BULLISH
        if total_score < -threshold:
            return Direction.BEARISH
        return Direction.NEUTRAL

    @staticmethod
    def _determine_strength(confidence: float) -> SignalStrength:
        if confidence >= CONFIDENCE_STRONG_THRESHOLD:
            return SignalStrength.STRONG
        if confidence >= CONFIDENCE_MODERATE_THRESHOLD:
            return SignalStrength.MODERATE
        return SignalStrength.WEAK
