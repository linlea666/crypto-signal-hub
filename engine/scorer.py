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
from engine.trade_advisor import derive_trade_suggestion

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

    def register_factor(self, factor: ScoreFactor) -> None:
        self._factors.append(factor)
        logger.info("注册评分因子: %s (满分 ±%.0f)", factor.name, factor.max_score)

    def evaluate(self, snapshot: MarketSnapshot) -> SignalReport:
        """执行完整评分流程，生成信号报告。"""
        # 1. 逐因子评分
        factor_scores = self._calculate_all_factors(snapshot)

        # 2. 汇总得分
        total_score = sum(fs.score for fs in factor_scores)
        max_possible = sum(fs.max_score for fs in factor_scores)

        # 3. 判断方向
        direction = self._determine_direction(total_score)

        # 4. 计算信心度
        confidence = calculate_confidence(factor_scores)

        # 5. 判断信号强度
        strength = self._determine_strength(confidence)

        # 6. 识别关键价位
        key_levels = identify_key_levels(snapshot)

        # 7. 推导交易建议（纯计算，从关键位和方向自动得出）
        trade_suggestion = derive_trade_suggestion(
            direction=direction,
            confidence=confidence,
            price=snapshot.price,
            levels=key_levels,
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
        )

    def _calculate_all_factors(
        self, snapshot: MarketSnapshot
    ) -> list[FactorScore]:
        """执行所有因子评分，单个因子异常不影响整体"""
        results: list[FactorScore] = []
        for factor in self._factors:
            factor_config = self._config.get_factor_config(factor.name)
            if not factor_config.enabled:
                continue
            try:
                score = factor.calculate(snapshot)
                results.append(score)
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
    def _determine_direction(total_score: float) -> Direction:
        # 微小得分视为中性，避免噪音信号
        if total_score > 5:
            return Direction.BULLISH
        if total_score < -5:
            return Direction.BEARISH
        return Direction.NEUTRAL

    @staticmethod
    def _determine_strength(confidence: float) -> SignalStrength:
        if confidence >= CONFIDENCE_STRONG_THRESHOLD:
            return SignalStrength.STRONG
        if confidence >= CONFIDENCE_MODERATE_THRESHOLD:
            return SignalStrength.MODERATE
        return SignalStrength.WEAK
