"""Order books.

``HwBook`` is the bit-exact model of rtl/book_engine.v + rtl/price_levels.v
(finite direct-mapped order table, fixed-depth sorted price levels, the same
collision / eviction / clamping rules).  ``RefBook`` is an unbounded
"textbook" book used only to measure how often the hardware's finite
structures change the top of book (fidelity), never for pass/fail.

Sides: 0 = bid (buy), 1 = ask (sell).
"""
from __future__ import annotations

from dataclasses import dataclass, field

M32 = 0xFFFFFFFF

ADD, REDUCE, DELETE, REPLACE = 1, 2, 3, 4


@dataclass(frozen=True)
class Event:
    """A decoded order-book message (the hardware 'record')."""
    kind: int
    msg_id: int
    ref: int
    side: int = 0
    shares: int = 0
    price: int = 0
    ref2: int = 0


def event_from_msg(msg_id: int, data: bytes, locate: int) -> Event | None:
    """Mirror of rtl/msg_decode.v: filter + extract, or None if not relevant."""
    if len(data) < 3 or ((data[1] << 8) | data[2]) != locate:
        return None
    t = chr(data[0])
    n = len(data)
    be = int.from_bytes
    if (t == "A" and n == 36) or (t == "F" and n == 40):
        return Event(ADD, msg_id, be(data[11:19], "big"), 1 if data[19] == ord("S") else 0,
                     be(data[20:24], "big"), be(data[32:36], "big"))
    if (t == "E" and n == 31) or (t == "C" and n == 36) or (t == "X" and n == 23):
        return Event(REDUCE, msg_id, be(data[11:19], "big"), 0, be(data[19:23], "big"))
    if t == "D" and n == 19:
        return Event(DELETE, msg_id, be(data[11:19], "big"))
    if t == "U" and n == 35:
        return Event(REPLACE, msg_id, be(data[11:19], "big"), 0, be(data[27:31], "big"),
                     be(data[31:35], "big"), be(data[19:27], "big"))
    return None


@dataclass
class Counters:
    relevant: int = 0
    collisions: int = 0
    misses: int = 0
    evictions: int = 0
    drops: int = 0


class HwBook:
    def __init__(self, order_bits: int = 12, depth: int = 8):
        self.order_bits = order_bits
        self.mask = (1 << order_bits) - 1
        self.depth = depth
        # slot -> (ref, side, price, shares) for valid entries only
        self.table: dict[int, tuple[int, int, int, int]] = {}
        # levels[side] = list of [price, qty], best first
        self.levels: list[list[list[int]]] = [[], []]
        self.c = Counters()

    # ---- price levels (rtl/price_levels.v) ----
    def _add(self, side: int, px: int, q: int) -> None:
        if q == 0:
            return
        lv = self.levels[side]
        for e in lv:
            if e[0] == px:
                e[1] = (e[1] + q) & M32
                return
        pos = None
        for i, e in enumerate(lv):
            if (px > e[0]) if side == 0 else (px < e[0]):
                pos = i
                break
        if pos is None:
            if len(lv) >= self.depth:
                self.c.drops += 1
                return
            pos = len(lv)
        lv.insert(pos, [px, q])
        if len(lv) > self.depth:
            lv.pop()
            self.c.evictions += 1

    def _sub(self, side: int, px: int, q: int) -> None:
        lv = self.levels[side]
        for i, e in enumerate(lv):
            if e[0] == px:
                if e[1] <= q:
                    del lv[i]
                else:
                    e[1] -= q
                return

    # ---- messages (rtl/book_engine.v FSM) ----
    def apply(self, ev: Event) -> None:
        self.c.relevant += 1
        slot = ev.ref & self.mask
        e = self.table.get(slot)
        hit = e is not None and e[0] == ev.ref
        if ev.kind == ADD:
            if e is None:
                self.table[slot] = (ev.ref, ev.side, ev.price, ev.shares)
                self._add(ev.side, ev.price, ev.shares)
            else:
                self.c.collisions += 1
        elif not hit:
            self.c.misses += 1
        elif ev.kind == REDUCE:
            ref, side, px, sh = e
            dec = min(ev.shares, sh)
            rem = sh - dec
            if rem:
                self.table[slot] = (ref, side, px, rem)
            else:
                del self.table[slot]
            self._sub(side, px, dec)
        elif ev.kind == DELETE:
            ref, side, px, sh = e
            del self.table[slot]
            self._sub(side, px, sh)
        else:  # REPLACE
            ref, side, px, sh = e
            del self.table[slot]
            self._sub(side, px, sh)
            slot2 = ev.ref2 & self.mask
            if slot2 not in self.table:
                self.table[slot2] = (ev.ref2, side, ev.price, ev.shares)
                self._add(side, ev.price, ev.shares)
            else:
                self.c.collisions += 1

    def snapshot(self, side: int) -> list[tuple[int, int]]:
        return [(p, q) for p, q in self.levels[side]]

    def top(self, side: int):
        lv = self.levels[side]
        return (lv[0][0], lv[0][1]) if lv else None


@dataclass
class RefBook:
    """Unbounded reference book (exact ITCH semantics)."""
    orders: dict = field(default_factory=dict)          # ref -> [side, px, sh]
    levels: list = field(default_factory=lambda: [{}, {}])  # side -> {px: qty}

    def _lv(self, side, px, dq):
        d = self.levels[side]
        v = d.get(px, 0) + dq
        if v > 0:
            d[px] = v
        else:
            d.pop(px, None)

    def apply(self, ev: Event) -> None:
        if ev.kind == ADD:
            if ev.ref in self.orders:
                return
            self.orders[ev.ref] = [ev.side, ev.price, ev.shares]
            self._lv(ev.side, ev.price, ev.shares)
            return
        o = self.orders.get(ev.ref)
        if o is None:
            return
        side, px, sh = o
        if ev.kind == REDUCE:
            dec = min(ev.shares, sh)
            o[2] -= dec
            self._lv(side, px, -dec)
            if o[2] == 0:
                del self.orders[ev.ref]
        elif ev.kind == DELETE:
            del self.orders[ev.ref]
            self._lv(side, px, -sh)
        else:
            del self.orders[ev.ref]
            self._lv(side, px, -sh)
            self.orders[ev.ref2] = [side, ev.price, ev.shares]
            self._lv(side, ev.price, ev.shares)

    def top(self, side: int):
        d = self.levels[side]
        if not d:
            return None
        px = max(d) if side == 0 else min(d)
        return (px, d[px])
