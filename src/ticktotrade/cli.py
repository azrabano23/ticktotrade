"""Command line interface.

  ticktotrade sim --messages N --seed S     one differential RTL-vs-golden run
  ticktotrade report                        full measurement suite -> results/
  ticktotrade mutate                        mutation smoke test only
  ticktotrade gen --messages N -o f.itch    write a synthetic binary ITCH file
  ticktotrade fetch-real                    download the pinned real ITCH excerpts to data/real/
  ticktotrade replay --itch-file F --symbols SPY,AAPL
                                            real ITCH file: golden over all of it, RTL bit-exact
  ticktotrade real-report                   replay every pinned source -> results/real_itch.json
"""
from __future__ import annotations

import argparse
import json
import sys


def _sim(a) -> int:
    from . import rtl, sim
    from .strategy import StrategyConfig
    if not rtl.have_iverilog():
        print("iverilog/vvp not found (sudo apt-get install -y iverilog)", file=sys.stderr)
        return 2
    cfg = sim.SimConfig(messages=a.messages, seed=a.seed, order_bits=a.order_bits,
                        depth=a.depth, gap_prob=a.gap_prob, ipg_max=a.ipg_max,
                        msgs_per_packet=a.msgs_per_packet, itch_file=a.itch_file,
                        strategy=StrategyConfig(locate=a.locate, imb_shift=a.imb_shift,
                                                max_spread=a.max_spread, max_qty=a.max_qty,
                                                stock=a.stock))
    r = sim.run_sim(cfg)
    if a.json:
        print(json.dumps(r, indent=1, default=str))
    else:
        gc = r["golden_counters"]
        print(f"{'PASS' if r['pass'] else 'FAIL'}: {gc['msgs']} msgs in {gc['pkts']} packets, "
              f"{r['snapshots_compared']} book snapshots and {r['orders_compared']} OUCH orders "
              f"bit-exact vs golden")
        print(f"  book: relevant={gc['relevant']} collisions={gc['collisions']} misses={gc['misses']} "
              f"evictions={gc['evictions']} drops={gc['drops']}  "
              f"BBO fidelity vs unbounded book={r['fidelity']['bbo_match_frac']:.4f}")
        lp, lm = r["latency_pkt_cycles"], r["latency_msg_cycles"]
        if lp:
            print(f"  tick-to-trade (pkt first byte -> order first byte): min/median/max = "
                  f"{lp['min']}/{lp['median']}/{lp['max']} cycles")
            print(f"  processing (msg last byte -> order first byte):     min/median/max = "
                  f"{lm['min']}/{lm['median']}/{lm['max']} cycles")
        t = r["throughput"]
        print(f"  throughput: {t['msgs_per_cycle']} msgs/cycle over {t['window_cycles']} cycles "
              f"(bus utilization {t['bus_utilization']}), FIFO max occupancy {t['fifo_max_occupancy']}")
        print(f"  outputs: {r['workdir']}")
        for e in r["errors"]:
            print("  ERROR:", e)
    return 0 if r["pass"] else 1


def _report(a) -> int:
    from . import report
    return report.main(skip_synth=a.skip_synth, skip_mutation=a.skip_mutation, quick=a.quick)


def _mutate(a) -> int:
    from . import mutation, rtl
    r = mutation.run_mutation(rtl.PROJECT_ROOT)
    for x in r["results"]:
        print(f"{x['status']:9s} {x['file']:15s} {x['desc']}")
    print(f"killed {r['killed']}/{r['mutants']}")
    return 0 if r["killed"] == r["mutants"] else 1


def _gen(a) -> int:
    from . import gen, itch
    msgs, _ = gen.generate(gen.GenConfig(n_messages=a.messages, seed=a.seed))
    itch.write_itch_file(a.output, msgs)
    print(f"wrote {len(msgs)} messages to {a.output}")
    return 0


def _fetch_real(a) -> int:
    from . import replay
    for name in a.sources or replay.SOURCES:
        src = replay.SOURCES[name]
        p = replay.fetch(src)
        print(f"{name}: {p} ({src.size:,} bytes, sha256 {src.sha256[:16]}... ok)")
    return 0


def _replay(a) -> int:
    from . import replay
    syms = [x for x in (a.symbols or "").split(",") if x] or None
    r = replay.replay_file(a.itch_file, syms, a.rtl_messages, a.order_bits, a.depth,
                           a.ref_hash, sweep=not a.no_sweep, run_rtl=not a.no_rtl,
                           strategy={"imb_shift": a.imb_shift, "max_spread": a.max_spread,
                                     "max_qty": a.max_qty})
    if a.json:
        print(json.dumps(r, indent=1, default=str))
    else:
        st = r["stream"]
        print(f"{r['file']}: {st['messages']:,} msgs, {st['packets']:,} MoldUDP64 packets, "
              f"sha256 {r['sha256']}")
        print("  mix: " + ", ".join(f"{k}={v}" for k, v in st["message_mix"].items()))
        print(f"  parser at line rate: {st['line_rate_msgs_per_cycle']} msgs/cycle "
              f"(mean {st['wire_bytes_per_msg']['mean']} wire bytes/msg)")
        for sym, s in r["symbols"].items():
            b = s["book"]
            print(f"  {sym} (locate {s['locate']}): {b['book_msgs']:,} book msgs, "
                  f"collisions={b['collisions']} misses={b['misses']} evictions={b['evictions']} "
                  f"drops={b['drops']} BBO match={b['bbo_match_frac']:.4f} "
                  f"orders={s['golden_full']['orders']}")
            x = s.get("rtl")
            if x:
                print(f"    RTL {'PASS' if x['pass'] else 'FAIL'} over {x['messages']:,} msgs: "
                      f"{x['snapshots_compared']:,} snapshots, {x['orders_compared']:,} orders "
                      f"bit-exact; msg->order cycles {x['latency_msg_cycles']}")
                for e in x["errors"]:
                    print("    ERROR:", e)
    bad = [s for s in r["symbols"].values() if "rtl" in s and not s["rtl"]["pass"]]
    return 1 if bad else 0


def _real_report(a) -> int:
    from . import report, replay
    res = replay.run_suite(a.sources or None, a.rtl_messages, allow_fetch=not a.no_fetch)
    out = report.RESULTS_DIR / "real_itch.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, default=str) + "\n")
    replay.write_readme(res)
    print(replay.real_table(res))
    print(f"wrote {out}")
    return 0 if res["all_pass"] else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ticktotrade", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sim", help="run one RTL vs golden differential simulation")
    s.add_argument("--messages", type=int, default=5000)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--order-bits", type=int, default=12)
    s.add_argument("--depth", type=int, default=8)
    s.add_argument("--gap-prob", type=float, default=0.1)
    s.add_argument("--ipg-max", type=int, default=3)
    s.add_argument("--msgs-per-packet", type=int, default=4)
    s.add_argument("--locate", type=int, default=1)
    s.add_argument("--stock", default="AAPL")
    s.add_argument("--imb-shift", type=int, default=1)
    s.add_argument("--max-spread", type=int, default=500)
    s.add_argument("--max-qty", type=int, default=500)
    s.add_argument("--itch-file", default=None,
                   help="replay a NASDAQ binary ITCH 5.0 file (len-prefixed, optionally .gz)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_sim)
    r = sub.add_parser("report", help="run the measurement suite and write results/")
    r.add_argument("--skip-synth", action="store_true")
    r.add_argument("--skip-mutation", action="store_true")
    r.add_argument("--quick", action="store_true", help="smaller runs (for smoke testing)")
    r.set_defaults(fn=_report)
    m = sub.add_parser("mutate", help="mutation smoke test of the verification flow")
    m.set_defaults(fn=_mutate)
    g = sub.add_parser("gen", help="write a synthetic binary ITCH 5.0 file")
    g.add_argument("--messages", type=int, default=10000)
    g.add_argument("--seed", type=int, default=1)
    g.add_argument("-o", "--output", required=True)
    g.set_defaults(fn=_gen)
    f = sub.add_parser("fetch-real", help="download the pinned real ITCH excerpts (sha256 checked)")
    f.add_argument("sources", nargs="*")
    f.set_defaults(fn=_fetch_real)
    rp = sub.add_parser("replay", help="replay a real ITCH 5.0 file: golden + bit-exact RTL")
    rp.add_argument("--itch-file", required=True,
                    help="length-prefixed ITCH 5.0 (NASDAQ sample format), optionally gzipped")
    rp.add_argument("--symbols", default=None,
                    help="comma separated symbols or locates (default: 3 busiest locates)")
    rp.add_argument("--rtl-messages", type=int, default=None,
                    help="RTL window: first N messages (default: whole file)")
    rp.add_argument("--order-bits", type=int, default=12)
    rp.add_argument("--depth", type=int, default=8)
    rp.add_argument("--ref-hash", type=int, default=1, choices=(0, 1),
                    help="order-table index: 1 XOR hash (default), 0 low reference bits")
    rp.add_argument("--imb-shift", type=int, default=1)
    rp.add_argument("--max-spread", type=int, default=500)
    rp.add_argument("--max-qty", type=int, default=500)
    rp.add_argument("--no-rtl", action="store_true", help="golden model only")
    rp.add_argument("--no-sweep", action="store_true")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(fn=_replay)
    rr = sub.add_parser("real-report", help="replay all pinned sources -> results/real_itch.json")
    rr.add_argument("sources", nargs="*")
    rr.add_argument("--rtl-messages", type=int, default=None)
    rr.add_argument("--no-fetch", action="store_true")
    rr.set_defaults(fn=_real_report)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
