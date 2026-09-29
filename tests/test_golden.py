"""Golden model semantics, one message type at a time (independent of the RTL)."""
import random

from ticktotrade import gen, golden
from ticktotrade.book import (ADD, DELETE, HASH_LOW_BITS, HASH_XOR, REDUCE, REF_HASH_MASKS, REPLACE,
                              Event, HwBook, RefBook, event_from_msg, ref_slot)
from ticktotrade.itch import encode
from ticktotrade.strategy import ImbalanceStrategy, StrategyConfig


def HB(order_bits=8, depth=4):
    """Book indexed by the low reference bits, so slots in these tests are just ref % 2**bits."""
    return HwBook(order_bits, depth, HASH_LOW_BITS)


def A(ref, side, sh, px):
    return Event(ADD, 0, ref, side, sh, px)


def test_add_and_levels_sorted():
    b = HB(order_bits=8, depth=4)
    for i, (side, px, sh) in enumerate([(0, 100, 10), (0, 102, 5), (0, 101, 7), (1, 105, 3),
                                        (1, 104, 4), (0, 102, 1)]):
        b.apply(A(i + 1, side, sh, px))
    assert b.snapshot(0) == [(102, 6), (101, 7), (100, 10)]
    assert b.snapshot(1) == [(104, 4), (105, 3)]
    assert len(b.table) == 6


def test_execute_partial_full_and_clamp():
    b = HB(8, 4)
    b.apply(A(1, 0, 100, 50))
    b.apply(Event(REDUCE, 0, 1, shares=30))               # E / C / X partial
    assert b.snapshot(0) == [(50, 70)] and b.table[1][3] == 70
    b.apply(Event(REDUCE, 0, 1, shares=1000))             # over-execute: clamp, remove
    assert b.snapshot(0) == [] and 1 not in b.table and b.c.misses == 0
    b.apply(Event(REDUCE, 0, 1, shares=1))                # now unknown
    assert b.c.misses == 1


def test_delete_known_and_unknown():
    b = HB(8, 4)
    b.apply(A(1, 1, 100, 50))
    b.apply(A(2, 1, 50, 50))
    b.apply(Event(DELETE, 0, 1))
    assert b.snapshot(1) == [(50, 50)]
    b.apply(Event(DELETE, 0, 999))
    assert b.c.misses == 1 and b.snapshot(1) == [(50, 50)]


def test_replace_moves_order_and_keeps_side():
    b = HB(4, 4)
    b.apply(A(3, 1, 100, 60))
    b.apply(Event(REPLACE, 0, 3, shares=40, price=61, ref2=19))   # 19 & 15 == 3: same slot
    assert b.snapshot(1) == [(61, 40)] and b.table == {3: (19, 1, 61, 40)}
    b.apply(A(1, 0, 10, 50))
    b.apply(Event(REPLACE, 0, 19, shares=5, price=62, ref2=17))   # slot 1 busy -> collision
    assert b.c.collisions == 1 and b.snapshot(1) == [] and 3 not in b.table


def test_collision_policy_drops_new_add():
    b = HB(4, 4)
    b.apply(A(1, 0, 10, 50))
    b.apply(A(17, 0, 20, 51))
    assert b.c.collisions == 1 and b.table[1][0] == 1 and b.snapshot(0) == [(50, 10)]
    b.apply(Event(DELETE, 0, 17))
    assert b.c.misses == 1


def test_level_eviction_and_drop():
    b = HB(8, 3)
    for i, px in enumerate([100, 99, 98]):
        b.apply(A(i + 1, 0, 10, px))
    b.apply(A(4, 0, 10, 97))                  # worse than all, side full -> drop
    assert b.c.drops == 1 and b.snapshot(0) == [(100, 10), (99, 10), (98, 10)]
    b.apply(A(5, 0, 10, 101))                 # better -> worst level evicted
    assert b.c.evictions == 1 and b.snapshot(0) == [(101, 10), (100, 10), (99, 10)]
    b.apply(Event(DELETE, 0, 3))              # order at evicted level: table hit, level no-op
    assert b.c.misses == 0 and 3 not in b.table and len(b.snapshot(0)) == 3


def test_zero_share_add_creates_no_level():
    b = HB(8, 4)
    b.apply(A(1, 1, 0, 10))
    assert b.snapshot(1) == [] and 1 in b.table


def test_event_filter_by_locate_type_and_length():
    a = encode({"type": "A", "stock_locate": 1, "order_ref": 5, "side": "B", "shares": 1,
                "stock": "X", "price": 2})
    assert event_from_msg(0, a, 1) == Event(ADD, 0, 5, 0, 1, 2)
    assert event_from_msg(0, a, 2) is None
    assert event_from_msg(0, a[:-1], 1) is None
    p = encode({"type": "P", "stock_locate": 1, "order_ref": 5})
    assert event_from_msg(0, p, 1) is None
    u = encode({"type": "U", "stock_locate": 1, "order_ref": 5, "new_order_ref": 6,
                "shares": 7, "price": 8})
    assert event_from_msg(3, u, 1) == Event(REPLACE, 3, 5, 0, 7, 8, 6)


def test_strategy_edge_triggered():
    s = ImbalanceStrategy(StrategyConfig(imb_shift=1, max_spread=500, max_qty=300))
    assert s.on_update(0, (1000, 400), (1100, 100)) is not None      # 4x bid-heavy -> buy
    assert s.on_update(1, (1000, 400), (1100, 100)) is None          # still true: no refire
    f = s.on_update(2, (1000, 100), (1100, 900))                     # ask-heavy -> sell
    assert (f.side, f.price, f.shares) == ("S", 1000, 100)
    assert s.on_update(3, (1000, 100), (1700, 900)) is None          # spread too wide
    f = s.on_update(4, (1000, 900), (1100, 400))
    assert (f.side, f.price, f.shares) == ("B", 1100, 300)           # capped by max_qty
    assert s.on_update(5, None, (1100, 1)) is None


def test_generator_book_never_crossed_and_deterministic():
    m1, p1 = gen.generate(gen.GenConfig(n_messages=4000, seed=11))
    m2, p2 = gen.generate(gen.GenConfig(n_messages=4000, seed=11))
    assert m1 == m2 and p1 == p2
    ref = RefBook()
    for i, m in enumerate(m1):
        ev = event_from_msg(i, m, 1)
        if ev:
            ref.apply(ev)
            bid, ask = ref.top(0), ref.top(1)
            if bid and ask:
                assert bid[0] < ask[0]


def test_golden_pipeline_counts():
    _, pk = gen.generate(gen.GenConfig(n_messages=3000, seed=4))
    r = golden.run_golden(pk, StrategyConfig())
    c = r.counters
    assert c["msgs"] == 3000 and c["parse_err"] == 0
    assert len(r.snapshots) == c["relevant"] > 1000
    assert c["orders"] == len(r.orders) > 0
    assert all(len(b) == 49 for _, b in r.orders)
    assert r.fidelity["bbo_match_frac"] > 0.9


# ---------------- order-table index hash (real-data regression) ----------------
def test_ref_hash_is_bijective_on_aligned_strided_runs():
    """The XOR hash spreads any aligned run of 2**bits references with stride
    1..16 (other bits fixed) over every slot."""
    for bits in (4, 6, 12, 16):
        n = 1 << bits
        for stride in (1, 2, 4, 8, 16):
            for hi in (0, 0x1234567):
                base = hi * n * stride + (stride - 1 if stride > 1 else 0)
                run = [base + stride * k for k in range(n)]
                assert len({ref_slot(r, bits) for r in run}) == n, (bits, stride)


def test_low_bit_index_wastes_table_on_stride4_refs():
    """What the real 2019-12-30 TotalView replay exposed: one symbol's refs are
    all 4k+1, so a low-bit index can only ever use a quarter of the table."""
    import random
    rng = random.Random(1)
    refs = sorted({4 * rng.randrange(32_000_000, 33_000_000) + 1 for _ in range(20000)})
    assert len({ref_slot(r, 12, HASH_LOW_BITS) for r in refs}) == 1024
    assert len({ref_slot(r, 12, HASH_XOR) for r in refs}) > 4000     # ~random: e^-4.9 unused


def test_ref_hash_masks_match_rtl():
    import re
    from ticktotrade import rtl
    v = (rtl.RTL_DIR / "book_engine.v").read_text()
    got = {int(i): int(h, 16) for i, h in re.findall(r"5'd(\d+): hmask = 64'h([0-9A-F]+);", v)}
    assert got == dict(enumerate(REF_HASH_MASKS))


def test_stride4_flow_collides_less_with_hash():
    r = {}
    for h in (HASH_LOW_BITS, HASH_XOR):
        msgs, pk = gen.generate(gen.GenConfig(n_messages=6000, seed=4, ref_stride=4,
                                              tracked_share=1.0, target_live=40))
        g = golden.run_golden(pk, StrategyConfig(locate=1), order_bits=6, depth=8, ref_hash=h)
        r[h] = g.counters["collisions"]
    assert r[HASH_XOR] < 0.6 * r[HASH_LOW_BITS], r
