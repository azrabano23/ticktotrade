"""End-to-end differential simulation: golden model vs RTL."""
from __future__ import annotations

import random
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import bus, gen, golden, itch, rtl
from .strategy import StrategyConfig


@dataclass
class SimConfig:
    messages: int = 5000
    seed: int = 1
    order_bits: int = 12
    depth: int = 8
    gap_prob: float = 0.1       # probability of an idle cycle before each beat
    ipg_max: int = 3            # idle cycles between packets (uniform 0..ipg_max)
    msgs_per_packet: int = 4
    tracked_share: float = 0.5
    filler_rate: float = 0.12
    unknown_ref_rate: float = 0.01
    target_live: int = 60
    itch_file: str | None = None
    strategy: StrategyConfig = field(default_factory=StrategyConfig)


def _stock_hex(s: str, n: int) -> str:
    return s.encode("ascii").ljust(n, b" ")[:n].hex()


def make_packets(cfg: SimConfig):
    if cfg.itch_file:
        msgs = list(itch.read_itch_file(cfg.itch_file, cfg.messages))
        pkts = gen.packetize(msgs, random.Random(cfg.seed), cfg.msgs_per_packet)
        return msgs, pkts
    gcfg = gen.GenConfig(n_messages=cfg.messages, seed=cfg.seed,
                         tracked_locate=cfg.strategy.locate,
                         tracked_share=cfg.tracked_share, filler_rate=cfg.filler_rate,
                         unknown_ref_rate=cfg.unknown_ref_rate, target_live=cfg.target_live,
                         msgs_per_packet=cfg.msgs_per_packet)
    return gen.generate(gcfg)


def compare(g: golden.GoldenResult, r: dict, stim: bus.Stimulus) -> list[str]:
    """Return a list of human readable mismatches (empty == bit exact)."""
    errs: list[str] = list(f"unexpected tb line: {e}" for e in r["errors"][:5])
    gs, rs = g.snapshots, r["snapshots"]
    if len(gs) != len(rs):
        errs.append(f"snapshot count golden={len(gs)} rtl={len(rs)}")
    for a, b in zip(gs, rs):
        if (a[0], a[1], a[2]) != (b[0], b[1], b[2]):
            errs.append(f"book mismatch at msg {a[0]}/{b[0]}: golden bids={a[1]} asks={a[2]} "
                        f"rtl bids={b[1]} asks={b[2]}")
            break
    go, ro = g.orders, r["orders"]
    if len(go) != len(ro):
        errs.append(f"order count golden={len(go)} rtl={len(ro)}")
    for (mid, gb), o in zip(go, ro):
        if mid != o["msg_id"] or gb != o["bytes"]:
            errs.append(f"OUCH mismatch msg {mid}/{o['msg_id']}: golden={gb.hex()} rtl={o['bytes'].hex()}")
            break
    # latency cross-check: RTL's internal counters vs cycle numbers derived
    # independently from the stimulus line indices
    by_id = {m[0]: m for m in g.msgs}
    for o in ro:
        _, pi, off, data = by_id[o["msg_id"]]
        exp_pkt = o["cycle"] - stim.pkt_first_line[pi]
        exp_msg = o["cycle"] - stim.byte_line(pi, off + len(data) - 1)
        if (o["lat_pkt"], o["lat_msg"]) != (exp_pkt, exp_msg):
            errs.append(f"latency mismatch msg {o['msg_id']}: rtl=({o['lat_pkt']},{o['lat_msg']}) "
                        f"tb=({exp_pkt},{exp_msg})")
            break
    gt = {s: e for s, e in g.table.items()}
    if gt != r["table"]:
        diff = set(gt.items()) ^ set(r["table"].items())
        errs.append(f"order table mismatch ({len(diff)} entries differ), e.g. {sorted(diff)[:2]}")
    rc = r["counters"]
    for k, v in g.counters.items():
        if rc.get(k) != v:
            errs.append(f"counter {k}: golden={v} rtl={rc.get(k)}")
    for k in ("fifo_ovf", "order_q_ovf", "busy_at_end"):
        if rc.get(k):
            errs.append(f"{k} = {rc.get(k)} (must be 0)")
    return errs


def _stats(xs):
    if not xs:
        return None
    return {"min": min(xs), "median": statistics.median(xs), "max": max(xs),
            "mean": round(statistics.fmean(xs), 3), "n": len(xs)}


def run_sim(cfg: SimConfig, workdir: Path | None = None) -> dict:
    msgs, pkts = make_packets(cfg)
    return run_packets(pkts, cfg, workdir)


def run_packets(pkts: list[bytes], cfg: SimConfig, workdir: Path | None = None) -> dict:
    """Run golden + RTL on an explicit list of MoldUDP64 packets and compare."""
    t0 = time.time()
    workdir = Path(workdir or rtl.BUILD_DIR / f"sim_seed{cfg.seed}")
    workdir.mkdir(parents=True, exist_ok=True)
    g = golden.run_golden(pkts, cfg.strategy, cfg.order_bits, cfg.depth)
    stim = bus.build_stimulus(pkts, random.Random(cfg.seed * 7919 + 1), cfg.gap_prob, cfg.ipg_max)
    stim_path = workdir / "stim.txt"
    out_path = workdir / "rtl_out.txt"
    stim.write(stim_path)
    t1 = time.time()
    vvp = rtl.compile_tb("tb", {"ORDER_BITS": cfg.order_bits, "DEPTH": cfg.depth})
    s = cfg.strategy
    rtl.run_vvp(vvp, {"STIM": stim_path, "OUT": out_path, "LOCATE": s.locate,
                      "ENABLE": int(s.enable), "SHIFT": s.imb_shift, "SPREAD": s.max_spread,
                      "MAXQTY": s.max_qty, "STOCK": _stock_hex(s.stock, 8),
                      "FIRM": _stock_hex(s.firm, 4), "DRAIN": 400})
    t2 = time.time()
    r = rtl.parse_top_output(out_path, cfg.depth)
    errs = compare(g, r, stim)
    rc = r["counters"]
    beats = sum(1 for ln in stim.lines if ln[0])
    in_bytes = sum(len(p) for p in pkts)
    # sustained window: first beat to last beat presented
    first = next(i for i, ln in enumerate(stim.lines) if ln[0])
    last = max(i for i, ln in enumerate(stim.lines) if ln[0])
    window = last - first + 1
    lat_pkt = [o["lat_pkt"] for o in r["orders"]]
    lat_msg = [o["lat_msg"] for o in r["orders"]]
    kinds: dict[str, int] = {}
    for _, _, _, m in g.msgs:
        kinds[chr(m[0])] = kinds.get(chr(m[0]), 0) + 1
    n_msgs = len(g.msgs)
    return {
        "config": {**{k: v for k, v in asdict(cfg).items() if k != "strategy"},
                   "strategy": asdict(cfg.strategy)},
        "pass": not errs,
        "errors": errs[:20],
        "golden_counters": g.counters,
        "rtl_counters": rc,
        "fidelity": g.fidelity,
        "message_mix": dict(sorted(kinds.items())),
        "snapshots_compared": len(g.snapshots),
        "orders_compared": len(g.orders),
        "latency_pkt_cycles": _stats(lat_pkt),
        "latency_msg_cycles": _stats(lat_msg),
        "latency_msg_hist": {str(k): lat_msg.count(k) for k in sorted(set(lat_msg))},
        "throughput": {
            "packets": len(pkts), "bytes": in_bytes, "beats": beats,
            "window_cycles": window, "sim_cycles": rc.get("cycles"),
            "msgs_per_cycle": round(n_msgs / window, 4),
            "book_msgs_per_cycle": round(g.counters["relevant"] / window, 4),
            "bus_utilization": round(beats / window, 4),
            "fifo_max_occupancy": rc.get("fifo_max"),
        },
        "wall_seconds": {"golden_and_stimulus": round(t1 - t0, 2), "rtl_sim": round(t2 - t1, 2)},
        "workdir": str(workdir),
    }


def random_parser_packets(seed: int, n_packets: int = 300, min_len: int = 7, max_len: int = 60,
                          bad_rate: float = 0.0) -> list[bytes]:
    """Adversarial framing: arbitrary message lengths (every lane alignment and
    split length prefixes occur), heartbeats, and optionally malformed packets
    (MsgLen < 7, truncated message / prefix / header)."""
    from . import mold
    rng = random.Random(seed)
    pkts = []
    seq = 1
    for _ in range(n_packets):
        k = rng.choice([0, 1, 1, 2, 3, 5, 8])
        msgs = []
        for _ in range(k):
            ln = rng.choice([min_len, min_len, 12, 19, 23, 36, 40, 50, rng.randint(min_len, max_len)])
            msgs.append(bytes([rng.choice(b"AFECXDUSRPQBINZ")]) +
                        bytes(rng.getrandbits(8) for _ in range(ln - 1)))
        pkt = mold.encode_packet(b"PARSERTEST", seq, msgs)
        seq += k
        if bad_rate and rng.random() < bad_rate:
            mode = rng.randint(0, 2)
            if mode == 0 and len(pkt) > 21:
                pkt = pkt[:rng.randint(1, len(pkt) - 1)]           # truncate anywhere
            elif mode == 1:
                pkt = pkt + (rng.randint(0, 6)).to_bytes(2, "big") + b"\xAA" * 9  # short MsgLen
            else:
                pkt = pkt[:rng.randint(1, 19)]                     # short header
        pkts.append(pkt)
    return pkts


def run_parser_check(pkts: list[bytes], seed: int = 0, gap_prob: float = 0.2,
                     workdir: Path | None = None) -> list[str]:
    """Drive tb_parser with `pkts` and compare every extracted message (id, length,
    captured bytes, sof/eom cycles) and the error count with the golden framing."""
    from . import mold
    workdir = Path(workdir or rtl.BUILD_DIR / f"parser_seed{seed}")
    workdir.mkdir(parents=True, exist_ok=True)
    stim = bus.build_stimulus(pkts, random.Random(seed), gap_prob, 2)
    stim.write(workdir / "stim.txt")
    vvp = rtl.compile_tb("tb_parser")
    rtl.run_vvp(vvp, {"STIM": workdir / "stim.txt", "OUT": workdir / "parser_out.txt"})
    out = rtl.parse_parser_output(workdir / "parser_out.txt")
    exp, n_err = [], 0
    for pi, pkt in enumerate(pkts):
        ms, err = mold.split_messages(pkt)
        n_err += int(err)
        for off, data in ms:
            exp.append((pi, off, data))
    errs = []
    if len(exp) != len(out["msgs"]):
        errs.append(f"message count golden={len(exp)} rtl={len(out['msgs'])}")
    for i, ((pi, off, data), m) in enumerate(zip(exp, out["msgs"])):
        cap = min(len(data), 40)
        want = (i, len(data), data[:cap], stim.pkt_first_line[pi],
                stim.byte_line(pi, off + len(data) - 1))
        got = (m["msg_id"], m["len"], m["bytes"][:cap], m["sof"], m["eom"])
        if want != got:
            errs.append(f"msg {i} (pkt {pi} off {off}): golden={want} rtl={got}")
            break
    c = out["counters"]
    if c.get("parse_err") != n_err:
        errs.append(f"parse_err golden={n_err} rtl={c.get('parse_err')}")
    if c.get("pkts") != len(pkts):
        errs.append(f"pkts golden={len(pkts)} rtl={c.get('pkts')}")
    return errs
