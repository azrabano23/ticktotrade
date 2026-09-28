"""RTL tests (Icarus Verilog).  Skipped when iverilog is not installed."""
import random

import pytest

from conftest import requires_iverilog
from ticktotrade import bus, gen, golden, rtl, scenarios, sim

pytestmark = requires_iverilog


# ---------------- parser unit tests (tb/tb_parser.v) ----------------
@pytest.mark.parametrize("seed,gap", [(1, 0.0), (2, 0.25)])
def test_parser_straddling_all_alignments(seed, gap):
    """Arbitrary message lengths (7..60) -> messages and split length prefixes
    start/end at every lane; heartbeats; with and without idle cycles."""
    pkts = sim.random_parser_packets(seed, 300)
    assert sim.run_parser_check(pkts, seed, gap) == []


def test_parser_malformed_packets():
    pkts = sim.random_parser_packets(7, 300, bad_rate=0.35)
    assert sim.run_parser_check(pkts, 7, 0.1) == []


def test_parser_min_length_split_prefix():
    """7-byte messages whose length prefix is split across beats exercise the
    case where a message both starts and ends in the same beat."""
    from ticktotrade import mold
    pkts = []
    for pad in range(8):
        msgs = [b"Z" * (7 + pad)] + [bytes([65 + i]) * 7 for i in range(9)]
        pkts.append(mold.encode_packet(b"S", 1, msgs))
    assert sim.run_parser_check(pkts, 0, 0.0) == []


# ---------------- full pipeline differential tests ----------------
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_pipeline_matches_golden_default_params(seed):
    r = sim.run_sim(sim.SimConfig(messages=2500, seed=seed))
    assert r["pass"], r["errors"]
    assert r["orders_compared"] > 0 and r["snapshots_compared"] > 800
    assert r["latency_msg_cycles"]["min"] == 4       # documented pipeline floor


@pytest.mark.parametrize("seed", [11, 12])
def test_pipeline_small_table_edge_cases(seed):
    """64-entry table + 4-level book: collisions, evictions, drops, misses."""
    r = sim.run_sim(sim.SimConfig(messages=2500, seed=seed, order_bits=6, depth=4,
                                  gap_prob=0.3, unknown_ref_rate=0.05))
    assert r["pass"], r["errors"]
    c = r["golden_counters"]
    assert c["collisions"] > 0 and c["evictions"] > 0 and c["drops"] > 0 and c["misses"] > 0


@pytest.mark.parametrize("per_pkt", [1, 3, 64])
def test_directed_edge_cases(per_pkt):
    pk = gen.packetize(scenarios.edge_case_messages(), random.Random(per_pkt), per_pkt,
                       heartbeat_rate=0.1)
    r = sim.run_packets(pk, sim.SimConfig(seed=per_pkt, order_bits=4, depth=4, gap_prob=0.3))
    assert r["pass"], r["errors"]
    c = r["golden_counters"]
    assert c["collisions"] == 2 and c["misses"] == 4 and c["evictions"] >= 1 and c["drops"] >= 1
    assert c["orders"] >= 2


def test_line_rate_no_gaps_keeps_up():
    r = sim.run_sim(sim.SimConfig(messages=4000, seed=5, gap_prob=0.0, ipg_max=0,
                                  msgs_per_packet=40, tracked_share=1.0, filler_rate=0.0))
    assert r["pass"], r["errors"]
    assert r["throughput"]["bus_utilization"] == 1.0
    assert r["rtl_counters"]["fifo_ovf"] == 0


def test_checker_detects_corruption():
    """The comparison must flag a single corrupted snapshot / OUCH byte / latency."""
    cfg = sim.SimConfig(messages=1500, seed=8)
    r = sim.run_sim(cfg)
    assert r["pass"]
    msgs, pkts = sim.make_packets(cfg)
    g = golden.run_golden(pkts, cfg.strategy, cfg.order_bits, cfg.depth)
    stim = bus.build_stimulus(pkts, random.Random(cfg.seed * 7919 + 1), cfg.gap_prob, cfg.ipg_max)
    out = rtl.parse_top_output(rtl.BUILD_DIR / "sim_seed8" / "rtl_out.txt", cfg.depth)
    assert sim.compare(g, out, stim) == []
    o = out["orders"][0]
    o["bytes"] = o["bytes"][:-1] + b"R"
    assert any("OUCH" in e for e in sim.compare(g, out, stim))
    out = rtl.parse_top_output(rtl.BUILD_DIR / "sim_seed8" / "rtl_out.txt", cfg.depth)
    out["orders"][0]["lat_msg"] += 1
    assert any("latency" in e for e in sim.compare(g, out, stim))
    out = rtl.parse_top_output(rtl.BUILD_DIR / "sim_seed8" / "rtl_out.txt", cfg.depth)
    mid, bids, asks = out["snapshots"][10]
    out["snapshots"][10] = (mid, bids[:-1], asks)
    assert any("book mismatch" in e for e in sim.compare(g, out, stim))
