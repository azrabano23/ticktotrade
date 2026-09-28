"""Command line interface.

  ticktotrade sim --messages N --seed S     one differential RTL-vs-golden run
  ticktotrade report                        full measurement suite -> results/
  ticktotrade mutate                        mutation smoke test only
  ticktotrade gen --messages N -o f.itch    write a synthetic binary ITCH file
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
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
