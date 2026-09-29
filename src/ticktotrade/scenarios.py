"""Directed edge-case scenarios (used by tests and the mutation smoke script).

Run with a small table and shallow book (order_bits=4 -> 16 slots, depth=4)
so that collisions, same-slot replaces, evictions and drops are all hit.

The scenario is written with "logical" references whose intended table slot
is ``ref % 16``.  :func:`edge_case_messages` maps each logical reference to a
wire reference that lands in that slot under the table's index hash, so the
same collisions happen whichever ``ref_hash`` the RTL is built with.
"""
from __future__ import annotations

from . import itch
from .book import HASH_XOR, ref_slot

ORDER_BITS = 4


def wire_ref(r: int, ref_hash: int = HASH_XOR, order_bits: int = ORDER_BITS) -> int:
    """A distinct reference per logical ref ``r`` whose slot is ``r % 2**order_bits``.

    The XOR hash is a bijection on every aligned block of 2**order_bits
    consecutive references, so block ``r`` holds exactly one such reference."""
    n = 1 << order_bits
    if ref_hash != HASH_XOR:
        return r
    return next(x for x in range(n * r, n * r + n)
                if ref_slot(x, order_bits, ref_hash) == r % n)

P = 10000  # $1.00


def _m(t, locate=1, **kw):
    return itch.encode({"type": t, "stock_locate": locate, "tracking": 0,
                        "timestamp": 34_200_000_000_000 + len(kw), "stock": "AAPL", **kw})


def add(ref, side, sh, px, locate=1, mpid=None):
    if mpid:
        return _m("F", locate, order_ref=ref, side=side, shares=sh, price=px, attribution=mpid)
    return _m("A", locate, order_ref=ref, side=side, shares=sh, price=px)


def edge_case_messages(ref_hash: int = HASH_XOR) -> list[bytes]:
    m = _logical_messages()
    out = []
    for raw in m:
        d = itch.decode(raw)
        if "raw" in d or d["type"] not in "AFECXDU":
            out.append(raw)
            continue
        d["order_ref"] = wire_ref(d["order_ref"], ref_hash)
        if d["type"] == "U":
            d["new_order_ref"] = wire_ref(d["new_order_ref"], ref_hash)
        out.append(itch.encode(d))
    return out


def _logical_messages() -> list[bytes]:
    m = []
    m.append(_m("S", 0, event_code="O"))
    m.append(_m("D", order_ref=999))                          # delete unknown -> miss
    m.append(add(1, "B", 100, 10 * P))
    m.append(add(2, "S", 200, 10 * P + 500))
    m.append(_m("E", order_ref=1, executed_shares=30, match=1))           # partial exec
    m.append(_m("C", order_ref=1, executed_shares=20, match=2, printable="Y",
                exec_price=10 * P + 100))                                  # exec w/ price
    m.append(_m("X", order_ref=2, cancelled_shares=50))                   # partial cancel
    m.append(_m("E", order_ref=2, executed_shares=1000, match=3))         # over-exec: clamp
    m.append(_m("X", order_ref=2, cancelled_shares=1))                    # now unknown
    m.append(add(3, "B", 300, 10 * P + 100))
    m.append(add(4, "B", 400, 10 * P + 200, mpid="GSCO"))                 # F message
    m.append(add(5, "B", 500, 10 * P - 100))
    m.append(add(6, "B", 600, 10 * P - 200))                              # side full -> drop
    m.append(add(7, "B", 700, 10 * P + 300))                              # better -> evict
    m.append(_m("D", order_ref=5))                                        # level was evicted
    m.append(add(8, "S", 100, 10 * P + 400))
    m.append(_m("U", order_ref=3, new_order_ref=19, shares=250, price=10 * P + 250))  # same slot
    m.append(_m("U", order_ref=7, new_order_ref=17, shares=10, price=10 * P))  # slot 1 busy
    m.append(add(33, "S", 100, 11 * P))                                   # collision (slot 1)
    m.append(_m("E", order_ref=33, executed_shares=1, match=4))           # miss
    m.append(_m("U", order_ref=12345, new_order_ref=12346, shares=1, price=P))  # miss
    m.append(add(9, "B", 1000, 10 * P, locate=2))                         # other symbol
    m.append(_m("P", order_ref=0, side="B", shares=100, price=10 * P, match=5))  # trade msg
    m.append(itch.encode({"type": "A", "stock_locate": 1, "tracking": 0, "timestamp": 0,
                          "order_ref": 40, "side": "B", "shares": 5, "stock": "AAPL",
                          "price": P})[:-1] + b"")                        # bad length: ignored
    m.append(add(10, "S", 0, 12 * P))                                     # zero-share add
    m.append(_m("D", order_ref=10))
    m.append(add(11, "S", 0xFFFFFFFF, 0x7FFFFFFF))                        # extreme values
    m.append(_m("X", order_ref=11, cancelled_shares=0xFFFFFFFE))
    # a replace chain
    m.append(add(12, "S", 300, 10 * P + 450))
    m.append(_m("U", order_ref=12, new_order_ref=28, shares=200, price=10 * P + 460))
    m.append(_m("U", order_ref=28, new_order_ref=29, shares=150, price=10 * P + 470))
    m.append(_m("E", order_ref=29, executed_shares=150, match=6))
    # imbalance setup -> strategy buy, then sell
    m.append(add(13, "B", 5000, 10 * P + 350))
    m.append(_m("D", order_ref=8))
    m.append(add(14, "S", 100, 10 * P + 400))
    m.append(_m("D", order_ref=13))
    m.append(add(15, "S", 5000, 10 * P + 380))
    m.append(add(16, "B", 100, 10 * P + 370))
    m.append(_m("D", order_ref=15))
    m.append(_m("S", 0, event_code="C"))
    return m
