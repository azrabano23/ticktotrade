"""Synthetic but market-shaped ITCH 5.0 generator.

Several symbols, each with its own limit order book driven by a random walk
mid price.  Order references are global and strictly increasing (as on
NASDAQ).  Flow per symbol: adds (A, some F with MPID) near the touch,
partial cancels (X), deletes (D, biased toward recent orders), executions
(E, some C with price) against the oldest order at the touch, replaces (U,
new reference, price moved a few ticks) - never crossing the book.  Filler
messages of the other ITCH types (and system/directory messages) are mixed
in.  A small fraction of E/X/D reference orders that do not exist (edge
case).  Messages are packed 1..N per MoldUDP64 packet.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from . import itch, mold

SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "SPY",
           "QQQ", "IWM", "AMD", "INTC", "NFLX", "ORCL", "CSCO", "PEP"]
TICK = 100  # $0.01 in ITCH 4-decimal price units
LOTS = [100] * 6 + [200] * 3 + [300, 400, 500, 500, 1000, 1000, 2000, 50, 25, 1, 75, 150]
MPIDS = ["GSCO", "MSCO", "JPMS", "CDRG", "VIRT", "UBSS"]
FILLER = "HYLPQBINKJhVW"


@dataclass
class GenConfig:
    n_messages: int = 10_000
    seed: int = 1
    n_symbols: int = 6
    tracked_locate: int = 1
    tracked_share: float = 0.5     # fraction of order flow on the tracked symbol
    filler_rate: float = 0.12      # fraction of non order-book messages
    unknown_ref_rate: float = 0.01
    target_live: int = 60          # live orders per symbol the flow reverts to
    mean_offset_ticks: float = 3.0
    max_offset_ticks: int = 25
    msgs_per_packet: int = 4       # max messages per packet (uniform 1..N)
    max_packet_bytes: int = 1400
    ref_stride: int = 1            # 4 mimics NASDAQ TotalView, where one symbol's refs share ref % 4


class _Sym:
    def __init__(self, locate: int, name: str, mid: int):
        self.locate = locate
        self.name = name
        self.mid = mid
        self.orders: dict[int, list] = {}     # ref -> [side, px, sh]
        self.refs: list[int] = []
        self.idx: dict[int, int] = {}
        self.lv = [{}, {}]

    def add(self, ref, side, px, sh):
        self.orders[ref] = [side, px, sh]
        self.idx[ref] = len(self.refs)
        self.refs.append(ref)
        self.lv[side][px] = self.lv[side].get(px, 0) + sh

    def reduce(self, ref, n):
        o = self.orders[ref]
        o[2] -= n
        d = self.lv[o[0]]
        d[o[1]] -= n
        if d[o[1]] <= 0:
            del d[o[1]]
        if o[2] <= 0:
            self.remove(ref, lv=False)

    def remove(self, ref, lv=True):
        o = self.orders.pop(ref)
        if lv:
            d = self.lv[o[0]]
            d[o[1]] -= o[2]
            if d[o[1]] <= 0:
                del d[o[1]]
        i = self.idx.pop(ref)
        last = self.refs.pop()
        if last != ref:
            self.refs[i] = last
            self.idx[last] = i

    def best(self, side):
        d = self.lv[side]
        if not d:
            return None
        return max(d) if side == 0 else min(d)


class MarketGen:
    def __init__(self, cfg: GenConfig):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        r = self.rng
        n = max(1, min(cfg.n_symbols, len(SYMBOLS)))
        self.syms = [_Sym(i + 1, SYMBOLS[i], r.randint(2000, 50000) * TICK) for i in range(n)]
        self.next_ref = r.randint(1, 1 << 20) * 4
        self.match = 1
        self.ts = 34_200_000_000_000  # 09:30:00 in ns since midnight

    # ------------------------------------------------------------------
    def _hdr(self, t, locate):
        self.ts += self.rng.randint(50, 40_000)
        return {"type": t, "stock_locate": locate, "tracking": self.rng.randint(0, 15),
                "timestamp": self.ts}

    def _price_for(self, s: _Sym, side: int) -> int:
        r = self.rng
        off = min(int(r.expovariate(1.0 / self.cfg.mean_offset_ticks)), self.cfg.max_offset_ticks)
        if side == 0:
            px = s.mid - off * TICK
            ba = s.best(1)
            if ba is not None:
                px = min(px, ba - TICK)
        else:
            px = s.mid + (off + 1) * TICK
            bb = s.best(0)
            if bb is not None:
                px = max(px, bb + TICK)
        return max(px, TICK)

    def _add(self, s: _Sym) -> bytes:
        r = self.rng
        side = r.randint(0, 1)
        px = self._price_for(s, side)
        sh = r.choice(LOTS)
        ref = self.next_ref
        self.next_ref += self.cfg.ref_stride
        s.add(ref, side, px, sh)
        m = self._hdr("F" if r.random() < 0.15 else "A", s.locate)
        m.update(order_ref=ref, side="S" if side else "B", shares=sh, stock=s.name, price=px)
        if m["type"] == "F":
            m["attribution"] = r.choice(MPIDS)
        return itch.encode(m)

    def _pick(self, s: _Sym, recent_bias: bool = False) -> int:
        r = self.rng
        n = len(s.refs)
        if recent_bias and r.random() < 0.6:
            # refs list is roughly insertion ordered (swap-remove perturbs it)
            return s.refs[r.randint(max(0, n - 1 - n // 4), n - 1)]
        return s.refs[r.randrange(n)]

    def _unknown_ref(self) -> int:
        return self.rng.randint(1, max(2, self.next_ref + 1000))

    def _book_msg(self, s: _Sym) -> bytes:
        r = self.rng
        cfg = self.cfg
        if r.random() < 0.02:
            s.mid += r.choice((-TICK, TICK))
            s.mid = max(s.mid, 10 * TICK)
        live = len(s.orders)
        p_add = 0.62 if live < cfg.target_live else 0.30
        if live < 3 or r.random() < p_add:
            return self._add(s)
        if r.random() < cfg.unknown_ref_rate / (1 - p_add):
            t = r.choice("DXE")
            m = self._hdr(t, s.locate)
            m["order_ref"] = self._unknown_ref()
            m["cancelled_shares"] = m["executed_shares"] = 100
            m["match"] = self.match
            return itch.encode(m)
        x = r.random()
        if x < 0.36:                                   # delete
            ref = self._pick(s, recent_bias=True)
            s.remove(ref)
            m = self._hdr("D", s.locate)
            m["order_ref"] = ref
            return itch.encode(m)
        if x < 0.50:                                   # partial cancel
            ref = self._pick(s)
            sh = s.orders[ref][2]
            n = r.randint(1, sh - 1) if sh > 1 else 1
            s.reduce(ref, n)
            m = self._hdr("X", s.locate)
            m.update(order_ref=ref, cancelled_shares=n)
            return itch.encode(m)
        if x < 0.80:                                   # execution at the touch
            side = r.randint(0, 1)
            if not s.lv[side]:
                side ^= 1
            px = s.best(side)
            ref = min(k for k, o in s.orders.items() if o[0] == side and o[1] == px)
            sh = s.orders[ref][2]
            n = sh if r.random() < 0.5 or sh == 1 else r.randint(1, sh - 1)
            s.reduce(ref, n)
            t = "C" if r.random() < 0.2 else "E"
            m = self._hdr(t, s.locate)
            m.update(order_ref=ref, executed_shares=n, match=self.match,
                     printable=r.choice("YN"), exec_price=px + r.choice((0, 0, TICK, -TICK)))
            self.match += 1
            return itch.encode(m)
        # replace
        ref = self._pick(s)
        side, px, _sh = s.orders[ref]
        s.remove(ref)
        npx = px + r.randint(-2, 2) * TICK
        if side == 0:
            ba = s.best(1)
            if ba is not None:
                npx = min(npx, ba - TICK)
        else:
            bb = s.best(0)
            if bb is not None:
                npx = max(npx, bb + TICK)
        npx = max(npx, TICK)
        nsh = r.choice(LOTS)
        nref = self.next_ref
        self.next_ref += self.cfg.ref_stride
        s.add(nref, side, npx, nsh)
        m = self._hdr("U", s.locate)
        m.update(order_ref=ref, new_order_ref=nref, shares=nsh, price=npx)
        return itch.encode(m)

    def _filler(self) -> bytes:
        r = self.rng
        t = r.choice(FILLER)
        s = r.choice(self.syms)
        m = self._hdr(t, s.locate)
        m.update(stock=s.name, order_ref=0, side=r.choice("BS"), shares=r.choice(LOTS),
                 price=s.mid, match=self.match, trading_state="T", reason="    ",
                 reg_sho_action="0", mpid=r.choice(MPIDS), primary_mm="N", mm_mode="N",
                 mp_state="A", cross_price=s.mid, cross_type=r.choice("OCHI"),
                 paired_shares=r.randint(0, 10**6), imbalance_shares=r.randint(0, 10**5),
                 imbalance_dir=r.choice("BSNO"), far_price=s.mid, near_price=s.mid,
                 current_ref_price=s.mid, price_variation="L", interest_flag="N",
                 ipo_release_qualifier="A", market_code="Q", halt_action="T",
                 level1=10**10, level2=9 * 10**9, level3=8 * 10**9, breached_level="1",
                 ref_price=s.mid, upper_price=s.mid + 50 * TICK,
                 lower_price=s.mid - 50 * TICK)
        self.match += 1
        return itch.encode(m)

    def _directory(self) -> list[bytes]:
        out = [itch.encode({**self._hdr("S", 0), "event_code": "O"})]
        for s in self.syms:
            m = self._hdr("R", s.locate)
            m.update(stock=s.name, market_category="Q", fin_status="N", round_lot_size=100,
                     round_lots_only="N", issue_class="C", issue_subtype="Z ",
                     authenticity="P", short_sale_threshold="N", ipo_flag=" ",
                     luld_tier="1", etp_flag="N", etp_leverage=0, inverse="N")
            out.append(itch.encode(m))
        return out

    # ------------------------------------------------------------------
    def messages(self) -> list[bytes]:
        cfg = self.cfg
        r = self.rng
        tracked = [s for s in self.syms if s.locate == cfg.tracked_locate]
        others = [s for s in self.syms if s.locate != cfg.tracked_locate] or self.syms
        out = self._directory()
        while len(out) < cfg.n_messages:
            if r.random() < cfg.filler_rate:
                out.append(self._filler())
                continue
            s = tracked[0] if tracked and r.random() < cfg.tracked_share else r.choice(others)
            out.append(self._book_msg(s))
        return out[:cfg.n_messages]


def packetize(msgs: list[bytes], rng: random.Random, max_msgs: int = 4,
              max_bytes: int = 1400, session: bytes = b"TTTSESSION", heartbeat_rate: float = 0.01):
    """Pack messages into MoldUDP64 packets of 1..max_msgs messages."""
    pkts = []
    seq = 1
    i = 0
    while i < len(msgs):
        if rng.random() < heartbeat_rate:
            pkts.append(mold.encode_packet(session, seq, []))
            continue
        k = rng.randint(1, max_msgs)
        group = []
        size = mold.HEADER_LEN
        while i < len(msgs) and len(group) < k and size + 2 + len(msgs[i]) <= max_bytes:
            group.append(msgs[i])
            size += 2 + len(msgs[i])
            i += 1
        pkts.append(mold.encode_packet(session, seq, group))
        seq += len(group)
    return pkts


def generate(cfg: GenConfig):
    """Return (messages, packets) deterministically from cfg.seed."""
    g = MarketGen(cfg)
    msgs = g.messages()
    pkts = packetize(msgs, random.Random(cfg.seed ^ 0x5EED), cfg.msgs_per_packet,
                     cfg.max_packet_bytes)
    return msgs, pkts
