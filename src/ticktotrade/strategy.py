"""Toy top-of-book imbalance strategy -- bit-exact model of rtl/strategy.v."""
from __future__ import annotations

from dataclasses import dataclass

M32 = 0xFFFFFFFF


@dataclass
class StrategyConfig:
    locate: int = 1
    enable: bool = True
    imb_shift: int = 1          # imbalance ratio = 2**imb_shift
    max_spread: int = 500       # in ITCH price units (1e-4 $): 500 = 5 cents
    max_qty: int = 500
    stock: str = "AAPL"
    firm: str = "TTRD"


@dataclass(frozen=True)
class Fire:
    msg_id: int
    side: str       # 'B' or 'S'
    price: int
    shares: int


class ImbalanceStrategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.prev_buy = False
        self.prev_sell = False

    def on_update(self, msg_id: int, bid, ask) -> Fire | None:
        """Called after every processed book message with top of book (px, qty) or None."""
        cfg = self.cfg
        both = (bid is not None and ask is not None
                and ((ask[0] - bid[0]) & M32) <= cfg.max_spread)
        buy_c = both and bid[1] >= (ask[1] << cfg.imb_shift)
        sell_c = both and ask[1] >= (bid[1] << cfg.imb_shift)
        buy_f = buy_c and not self.prev_buy
        sell_f = sell_c and not self.prev_sell and not buy_f
        self.prev_buy, self.prev_sell = buy_c, sell_c
        if not cfg.enable or not (buy_f or sell_f):
            return None
        if buy_f:
            return Fire(msg_id, "B", ask[0], min(ask[1], cfg.max_qty))
        return Fire(msg_id, "S", bid[0], min(bid[1], cfg.max_qty))
