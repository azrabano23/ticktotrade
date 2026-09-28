"""Golden model of the whole pipeline: packets in -> snapshots/orders/counters out."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import mold, ouch
from .book import HwBook, RefBook, event_from_msg
from .strategy import ImbalanceStrategy, StrategyConfig


@dataclass
class GoldenResult:
    msgs: list = field(default_factory=list)        # (msg_id, pkt_idx, offset, bytes)
    snapshots: list = field(default_factory=list)   # (msg_id, bids, asks)
    orders: list = field(default_factory=list)      # (msg_id, 49 bytes)
    fires: list = field(default_factory=list)
    counters: dict = field(default_factory=dict)
    table: dict = field(default_factory=dict)
    fidelity: dict = field(default_factory=dict)


def run_golden(packets: list[bytes], cfg: StrategyConfig, order_bits: int = 12,
               depth: int = 8) -> GoldenResult:
    res = GoldenResult()
    book = HwBook(order_bits, depth)
    ref = RefBook()
    strat = ImbalanceStrategy(cfg)
    parse_err = 0
    msg_id = 0
    token = 1
    bbo_match = 0
    for pi, pkt in enumerate(packets):
        msgs, err = mold.split_messages(pkt)
        parse_err += int(err)
        for off, data in msgs:
            res.msgs.append((msg_id, pi, off, data))
            ev = event_from_msg(msg_id, data, cfg.locate)
            msg_id += 1
            if ev is None:
                continue
            book.apply(ev)
            ref.apply(ev)
            res.snapshots.append((ev.msg_id, book.snapshot(0), book.snapshot(1)))
            bid, ask = book.top(0), book.top(1)
            bbo_match += int(bid == ref.top(0) and ask == ref.top(1))
            f = strat.on_update(ev.msg_id, bid, ask)
            if f is not None:
                res.fires.append(f)
                res.orders.append((f.msg_id, ouch.encode_enter_order(
                    ouch.token_for(token), f.side, f.shares, cfg.stock, f.price,
                    firm=cfg.firm)))
                token += 1
    c = book.c
    res.counters = {"msgs": msg_id, "pkts": len(packets), "parse_err": parse_err,
                    "relevant": c.relevant, "collisions": c.collisions,
                    "misses": c.misses, "evictions": c.evictions, "drops": c.drops,
                    "orders": len(res.orders)}
    res.table = dict(book.table)
    n = max(1, c.relevant)
    res.fidelity = {"bbo_match_frac": bbo_match / n, "book_updates": c.relevant,
                    "ref_live_orders_end": len(ref.orders),
                    "hw_live_orders_end": len(book.table)}
    return res
