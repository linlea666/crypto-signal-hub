"""交易所执行客户端。

基于 ccxt.async_support 封装 OKX 合约交易，
支持 demo/live 模式切换和原子性止盈止损下单。
"""

from __future__ import annotations

import logging
from typing import Any

import ccxt.async_support as ccxt

from config.schema import ExecutorConfig

logger = logging.getLogger(__name__)


class ExchangeClient:
    """OKX 合约交易执行客户端"""

    def __init__(self, config: ExecutorConfig):
        self._config = config
        self._exchange: ccxt.Exchange | None = None

    @property
    def is_connected(self) -> bool:
        return self._exchange is not None

    @property
    def mode_label(self) -> str:
        return "模拟盘" if self._config.mode == "demo" else "实盘"

    async def initialize(self) -> None:
        ex_class = getattr(ccxt, self._config.exchange, None)
        if ex_class is None:
            raise ValueError(f"不支持的交易所: {self._config.exchange}")

        self._exchange = ex_class({
            "apiKey": self._config.api_key,
            "secret": self._config.api_secret,
            "password": self._config.passphrase,
            "sandbox": self._config.mode == "demo",
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
            },
        })
        logger.info(
            "执行客户端初始化: %s (%s)",
            self._config.exchange, self.mode_label,
        )

    async def cleanup(self) -> None:
        if self._exchange:
            try:
                await self._exchange.close()
            except Exception as e:
                logger.debug("交易所连接关闭异常: %s", e)
            self._exchange = None

    async def test_connection(self) -> dict:
        """测试交易所连接，返回账户余额摘要"""
        if not self._exchange:
            return {"ok": False, "error": "客户端未初始化"}
        try:
            balance = await self._exchange.fetch_balance({"type": "swap"})
            usdt = balance.get("USDT", {})
            return {
                "ok": True,
                "mode": self._config.mode,
                "exchange": self._config.exchange,
                "equity": float(usdt.get("total", 0) or 0),
                "available": float(usdt.get("free", 0) or 0),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def get_balance(self) -> dict:
        """获取 USDT 余额信息"""
        if not self._exchange:
            return {"equity": 0, "available": 0}
        try:
            balance = await self._exchange.fetch_balance({"type": "swap"})
            usdt = balance.get("USDT", {})
            return {
                "equity": float(usdt.get("total", 0) or 0),
                "available": float(usdt.get("free", 0) or 0),
            }
        except Exception as e:
            logger.error("获取余额失败: %s", e)
            return {"equity": 0, "available": 0}

    async def get_positions(self) -> list[dict]:
        """获取当前合约持仓"""
        if not self._exchange:
            return []
        try:
            positions = await self._exchange.fetch_positions()
            return [
                {
                    "symbol": p["symbol"],
                    "side": p["side"],
                    "contracts": float(p.get("contracts", 0) or 0),
                    "notional": float(p.get("notional", 0) or 0),
                    "unrealizedPnl": float(p.get("unrealizedPnl", 0) or 0),
                    "entryPrice": float(p.get("entryPrice", 0) or 0),
                    "leverage": int(p.get("leverage", 1) or 1),
                }
                for p in positions
                if float(p.get("contracts", 0) or 0) > 0
            ]
        except Exception as e:
            logger.error("获取持仓失败: %s", e)
            return []

    async def get_open_orders(self, symbol: str = "") -> list[dict]:
        """获取当前挂单"""
        if not self._exchange:
            return []
        try:
            sym = symbol.replace("/USDT", "/USDT:USDT") if symbol else None
            orders = await self._exchange.fetch_open_orders(sym)
            return [
                {
                    "id": o["id"],
                    "symbol": o["symbol"],
                    "side": o["side"],
                    "price": float(o.get("price", 0) or 0),
                    "amount": float(o.get("amount", 0) or 0),
                    "status": o.get("status"),
                }
                for o in orders
            ]
        except Exception as e:
            logger.error("获取挂单失败: %s", e)
            return []

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """设置合约杠杆"""
        if not self._exchange:
            return False
        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        try:
            await self._exchange.set_leverage(leverage, swap_symbol)
            return True
        except Exception as e:
            logger.warning("设置杠杆失败 %s x%d: %s", symbol, leverage, e)
            return False

    async def place_order_with_sl_tp(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        stop_loss: float,
        take_profit: float | None = None,
        leverage: int = 3,
    ) -> dict[str, Any]:
        """原子性下单：开仓 + 附带止损(+可选止盈) 一次 API 调用。

        take_profit=None 时仅附带 SL（用于限价单场景，TP 由移动止盈管理）。
        """
        if not self._exchange:
            return {"ok": False, "error": "客户端未初始化"}

        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")

        lev_ok = await self.set_leverage(symbol, leverage)
        if not lev_ok:
            return {"ok": False, "error": f"设置杠杆失败 {symbol} x{leverage}"}

        pos_side = "long" if side == "buy" else "short"

        algo_ord: dict[str, str] = {
            "slTriggerPx": str(stop_loss),
            "slOrdPx": "-1",
        }
        if take_profit is not None:
            algo_ord["tpTriggerPx"] = str(take_profit)
            algo_ord["tpOrdPx"] = "-1"

        try:
            order = await self._exchange.create_order(
                symbol=swap_symbol,
                type="limit",
                side=side,
                amount=amount,
                price=price,
                params={
                    "tdMode": "cross",
                    "posSide": pos_side,
                    "attachAlgoOrds": [algo_ord],
                },
            )
            order_id = order.get("id", "")
            tp_label = f"TP={take_profit:.2f}" if take_profit else "TP=移动止盈"
            logger.info(
                "下单成功 [%s] %s %s %.4f @ %.2f | SL=%.2f %s | %s",
                self.mode_label, side, swap_symbol, amount, price,
                stop_loss, tp_label, order_id,
            )
            return {"ok": True, "order_id": order_id}
        except Exception as e:
            logger.error("下单失败 %s %s: %s", side, swap_symbol, e)
            return {"ok": False, "error": str(e)}

    async def get_order_status(self, symbol: str, order_id: str) -> dict:
        """查询单个订单状态（用于限价单成交检测）"""
        if not self._exchange:
            return {"status": "unknown", "filled": 0, "avg_price": 0}
        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        try:
            order = await self._exchange.fetch_order(order_id, swap_symbol)
            return {
                "status": order.get("status", "unknown"),
                "filled": float(order.get("filled", 0) or 0),
                "avg_price": float(order.get("average", 0) or order.get("price", 0) or 0),
            }
        except Exception as e:
            logger.warning("查询订单状态失败 %s: %s", order_id, e)
            return {"status": "unknown", "filled": 0, "avg_price": 0}

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """取消订单"""
        if not self._exchange:
            return False
        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        try:
            await self._exchange.cancel_order(order_id, swap_symbol)
            logger.info("取消订单: %s %s", swap_symbol, order_id)
            return True
        except Exception as e:
            logger.error("取消订单失败 %s: %s", order_id, e)
            return False

    async def reduce_position(self, symbol: str, side: str, ratio: float) -> bool:
        """市价减仓指定比例（用于 TP1 部分平仓）"""
        if not self._exchange or ratio <= 0:
            return False
        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        close_side = "sell" if side == "buy" else "buy"
        pos_side = "long" if side == "buy" else "short"
        try:
            positions = await self._exchange.fetch_positions([swap_symbol])
            pos = next(
                (p for p in positions if p["side"] == pos_side and float(p.get("contracts", 0) or 0) > 0),
                None,
            )
            if not pos:
                logger.warning("减仓失败: 未找到持仓 %s %s", swap_symbol, pos_side)
                return False

            total = float(pos["contracts"])
            reduce_amount = round(total * min(ratio, 1.0), 6)
            if reduce_amount <= 0:
                return False

            await self._exchange.create_order(
                symbol=swap_symbol,
                type="market",
                side=close_side,
                amount=reduce_amount,
                params={"tdMode": "cross", "posSide": pos_side, "reduceOnly": True},
            )
            logger.info("减仓成功 %s %s %.4f/%.4f (%.0f%%)", swap_symbol, pos_side, reduce_amount, total, ratio * 100)
            return True
        except Exception as e:
            logger.error("减仓失败 %s %s: %s", swap_symbol, pos_side, e)
            return False

    async def close_position(self, symbol: str, side: str) -> bool:
        """市价平仓"""
        if not self._exchange:
            return False
        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        close_side = "sell" if side == "long" else "buy"
        pos_side = side
        try:
            positions = await self._exchange.fetch_positions([swap_symbol])
            pos = next(
                (p for p in positions if p["side"] == side and float(p.get("contracts", 0) or 0) > 0),
                None,
            )
            if not pos:
                logger.warning("未找到持仓 %s %s", swap_symbol, side)
                return False

            amount = float(pos["contracts"])
            await self._exchange.create_order(
                symbol=swap_symbol,
                type="market",
                side=close_side,
                amount=amount,
                params={"tdMode": "cross", "posSide": pos_side, "reduceOnly": True},
            )
            logger.info("平仓成功 %s %s %.4f", swap_symbol, side, amount)
            return True
        except Exception as e:
            logger.error("平仓失败 %s %s: %s", swap_symbol, side, e)
            return False

    async def get_market_price(self, symbol: str) -> float:
        """获取当前市场价"""
        if not self._exchange:
            return 0.0
        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        try:
            ticker = await self._exchange.fetch_ticker(swap_symbol)
            return float(ticker.get("last") or 0)
        except Exception:
            return 0.0

    async def fetch_algo_orders(self, symbol: str) -> list[dict]:
        """获取当前生效的 SL/TP 算法单"""
        if not self._exchange:
            return []
        try:
            response = await self._exchange.private_get_trade_orders_algo_pending({
                "instId": self._to_okx_inst_id(symbol),
                "ordType": "conditional",
            })
            data = response.get("data", [])
            return [
                {
                    "algoId": item.get("algoId", ""),
                    "instId": item.get("instId", ""),
                    "side": item.get("side", ""),
                    "posSide": item.get("posSide", ""),
                    "slTriggerPx": item.get("slTriggerPx", ""),
                    "tpTriggerPx": item.get("tpTriggerPx", ""),
                    "ordType": item.get("ordType", ""),
                }
                for item in data
            ]
        except Exception as e:
            logger.debug("获取算法单失败 %s: %s", symbol, e)
            return []

    async def amend_stop_loss(self, symbol: str, algo_id: str, new_sl: float) -> bool:
        """修改止损价格（OKX amendAlgoOrder）"""
        if not self._exchange:
            return False
        try:
            await self._exchange.private_post_trade_amend_algos({
                "instId": self._to_okx_inst_id(symbol),
                "algoId": algo_id,
                "newSlTriggerPx": str(new_sl),
            })
            logger.info("修改止损成功 %s algoId=%s → SL=%.2f", symbol, algo_id, new_sl)
            return True
        except Exception as e:
            logger.warning("修改止损失败 %s: %s", symbol, e)
            return False

    def _to_okx_inst_id(self, symbol: str) -> str:
        """统一转换为 OKX instId 格式: BTC/USDT → BTC-USDT-SWAP"""
        base = symbol.replace("/USDT", "").replace(":USDT", "")
        return f"{base}-USDT-SWAP"

    async def set_take_profit(
        self, symbol: str, side: str, tp_price: float, close_ratio: float = 1.0,
    ) -> bool:
        """为已有持仓设置止盈算法单（OKX conditional order）

        close_ratio < 1.0 时为部分止盈（closeFraction）。
        """
        if not self._exchange:
            return False
        inst_id = self._to_okx_inst_id(symbol)
        pos_side = "long" if side == "buy" else "short"
        close_side = "sell" if side == "buy" else "buy"

        params: dict = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": close_side,
            "posSide": pos_side,
            "ordType": "conditional",
            "tpTriggerPx": str(tp_price),
            "tpOrdPx": "-1",
        }

        if 0 < close_ratio < 1.0:
            params["closeFraction"] = str(round(close_ratio, 2))
        else:
            params["closeFraction"] = "1"

        try:
            await self._exchange.private_post_trade_order_algo(params)
            logger.info(
                "设置止盈成功 %s %s TP=%.2f (平%.0f%%)",
                symbol, pos_side, tp_price, close_ratio * 100,
            )
            return True
        except Exception as e:
            logger.warning("设置止盈失败 %s: %s", symbol, e)
            return False

    async def get_recent_fills(self, symbol: str, limit: int = 10) -> list[dict]:
        """获取最近成交记录，用于确定实际平仓价格。"""
        if not self._exchange:
            return []
        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        try:
            trades = await self._exchange.fetch_my_trades(swap_symbol, limit=limit)
            return [
                {
                    "price": float(t.get("price", 0) or 0),
                    "amount": float(t.get("amount", 0) or 0),
                    "side": t.get("side", ""),
                    "timestamp": t.get("timestamp", 0),
                    "datetime": t.get("datetime", ""),
                    "fee": t.get("fee", {}),
                }
                for t in trades
            ]
        except Exception as e:
            logger.debug("获取成交记录失败 %s: %s", symbol, e)
            return []

    async def get_min_order_amount(self, symbol: str) -> float:
        """获取最小下单数量"""
        if not self._exchange:
            return 0.0
        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        try:
            await self._exchange.load_markets()
            market = self._exchange.market(swap_symbol)
            return float(market.get("limits", {}).get("amount", {}).get("min", 0) or 0)
        except Exception:
            return 0.0
