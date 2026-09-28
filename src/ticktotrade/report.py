"""Measurement suite -> results/results.json + results/REPORT.md (+ README block)."""
from __future__ import annotations

import datetime as _dt
import json
import platform
import random
import subprocess
import time
from pathlib import Path

from . import gen, itch, mold, rtl, scenarios, sim, synth

RESULTS_DIR = rtl.PROJECT_ROOT / "results"
README = rtl.PROJECT_ROOT / "README.md"
BEGIN, END = "<!-- RESULTS:BEGIN -->", "<!-- RESULTS:END -->"

# Assumed clocks -- NOT timed.  No place & route or static timing was run.
CLOCKS_MHZ = {
    "156.25 MHz (10GbE 64-bit datapath)": 156.25,
    "250 MHz": 250.0,
    "322.27 MHz (10/25G GT-derived clock)": 322.265625,
}


def _ns(cycles, mhz):
    return round(cycles * 1000.0 / mhz, 1)


def _mk(t, locate=1, **kw):
    return itch.encode({"type": t, "stock_locate": locate, "tracking": 0, "timestamp": 1,
                        "stock": "AAPL", **kw})


def parser_worst_packets(n_msgs: int) -> list[bytes]:
    """Back-to-back minimum-size (12 byte) ITCH messages, 100 per packet."""
    msgs = [_mk("S" if i % 2 else "W", 0, event_code="O", breached_level="1")
            for i in range(n_msgs)]
    return [mold.encode_packet(b"WORSTCASE ", 1 + i, msgs[i:i + 100])
            for i in range(0, n_msgs, 100)]


def book_worst_packets(n_msgs: int) -> list[bytes]:
    """Every message is a book message for the tracked symbol, smallest sizes
    first: deletes of unknown orders (21 B on the wire -> II=2 engine path)
    interleaved with add/replace/replace/delete chains (replace hits take 3 cycles)."""
    msgs, ref = [], 1_000_000
    while len(msgs) < n_msgs:
        msgs += [_mk("D", order_ref=7), _mk("D", order_ref=9), _mk("D", order_ref=11)]
        msgs += [_mk("A", order_ref=ref, side="B", shares=100, price=1_000_000),
                 _mk("U", order_ref=ref, new_order_ref=ref + 1, shares=200, price=1_000_100),
                 _mk("U", order_ref=ref + 1, new_order_ref=ref + 2, shares=300, price=1_000_200),
                 _mk("D", order_ref=ref + 2)]
        ref += 3
    msgs = msgs[:n_msgs]
    return [mold.encode_packet(b"WORSTCASE ", 1 + i, msgs[i:i + 60])
            for i in range(0, len(msgs), 60)]


def _run(name, fn, log):
    t0 = time.time()
    r = fn()
    log(f"[{name}] {'PASS' if r.get('pass', True) else 'FAIL'} ({time.time() - t0:.1f}s)")
    return r


def _slim(r: dict) -> dict:
    keep = ("pass", "errors", "golden_counters", "fidelity", "message_mix", "snapshots_compared",
            "orders_compared", "latency_pkt_cycles", "latency_msg_cycles", "latency_msg_hist",
            "throughput", "config", "wall_seconds")
    out = {k: r[k] for k in keep if k in r}
    rc = r.get("rtl_counters", {})
    out["rtl_counters"] = {k: v for k, v in rc.items() if k != "tob"}
    return out


def run_suite(skip_synth=False, skip_mutation=False, quick=False, log=print) -> dict:
    n = (lambda full, q: q if quick else full)
    res: dict = {
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": {
            "python": platform.python_version(),
            "iverilog": subprocess.run(["iverilog", "-V"], capture_output=True,
                                       text=True).stdout.splitlines()[0],
        },
        "assumed_clocks_mhz": CLOCKS_MHZ,
        "design_params": {"ORDER_BITS": 12, "order_table_entries": 4096, "DEPTH": 8,
                          "bus_bytes_per_cycle": 8, "capture_bytes": 40, "input_fifo": 8,
                          "order_queue": 8},
    }
    runs = {}
    runs["latency"] = _slim(_run("latency", lambda: sim.run_sim(sim.SimConfig(
        messages=n(30000, 3000), seed=1)), log))
    runs["line_rate"] = _slim(_run("line_rate", lambda: sim.run_sim(sim.SimConfig(
        messages=n(20000, 3000), seed=2, gap_prob=0.0, ipg_max=0, msgs_per_packet=40,
        tracked_share=1.0, filler_rate=0.0)), log))
    runs["parser_worst"] = _slim(_run("parser_worst", lambda: sim.run_packets(
        parser_worst_packets(n(20000, 2000)), sim.SimConfig(seed=3, gap_prob=0.0, ipg_max=0),
        rtl.BUILD_DIR / "parser_worst"), log))
    runs["book_worst"] = _slim(_run("book_worst", lambda: sim.run_packets(
        book_worst_packets(n(20000, 2000)), sim.SimConfig(seed=4, gap_prob=0.0, ipg_max=0),
        rtl.BUILD_DIR / "book_worst"), log))
    res["runs"] = runs

    # regression: many seeds with a tiny table / shallow book (lots of edge cases)
    reg = []
    for seed in range(1, n(9, 3)):
        r = sim.run_sim(sim.SimConfig(messages=n(4000, 1000), seed=100 + seed, order_bits=6,
                                      depth=4))
        reg.append({"seed": 100 + seed, "pass": r["pass"], "errors": r["errors"][:2],
                    "snapshots": r["snapshots_compared"], "orders": r["orders_compared"],
                    **{k: r["golden_counters"][k] for k in ("collisions", "misses",
                                                            "evictions", "drops")}})
    pk = gen.packetize(scenarios.edge_case_messages(), random.Random(3), 3, heartbeat_rate=0.1)
    d = sim.run_packets(pk, sim.SimConfig(seed=3, order_bits=4, depth=4, gap_prob=0.3))
    par = []
    for seed, bad in ((1, 0.0), (2, 0.0), (3, 0.3), (4, 0.3)):
        pkts = sim.random_parser_packets(seed, n(500, 100), bad_rate=bad)
        par.append({"seed": seed, "bad_rate": bad, "packets": len(pkts),
                    "errors": sim.run_parser_check(pkts, seed, 0.15)})
    res["regression"] = {"random_small_table": reg, "directed_edge_cases": {
        "pass": d["pass"], "errors": d["errors"], "counters": d["golden_counters"]},
        "parser_adversarial": par}
    log(f"[regression] {sum(x['pass'] for x in reg)}/{len(reg)} seeds, directed "
        f"{'PASS' if d['pass'] else 'FAIL'}, parser {sum(not x['errors'] for x in par)}/{len(par)}")

    if not skip_synth and synth.have_yosys():
        log("[synth] yosys synth_xilinx (takes ~1-2 min)...")
        res["synth"] = synth.run_synth(RESULTS_DIR)
    else:
        prev = RESULTS_DIR / "results.json"
        if prev.exists() and "synth" in json.loads(prev.read_text()):
            res["synth"] = json.loads(prev.read_text())["synth"]
            res["synth"]["note"] = "carried over from a previous report run"
    if not skip_mutation:
        from . import mutation
        log("[mutation] running mutants (~2-3 min)...")
        res["mutation"] = mutation.run_mutation(rtl.PROJECT_ROOT)
    else:
        prev = RESULTS_DIR / "results.json"
        if prev.exists() and "mutation" in json.loads(prev.read_text()):
            res["mutation"] = json.loads(prev.read_text())["mutation"]
    return res


# ---------------------------------------------------------------------------
def headline(res: dict) -> dict:
    lat = res["runs"]["latency"]
    lr = res["runs"]["line_rate"]
    return {
        "lat_pkt": lat["latency_pkt_cycles"],
        "lat_msg": lat["latency_msg_cycles"],
        "lr_lat_msg": lr["latency_msg_cycles"],
        "msgs_per_cycle_line_rate": lr["throughput"]["msgs_per_cycle"],
        "parser_worst_msgs_per_cycle": res["runs"]["parser_worst"]["throughput"]["msgs_per_cycle"],
        "book_worst_msgs_per_cycle": res["runs"]["book_worst"]["throughput"]["msgs_per_cycle"],
    }


def results_table(res: dict) -> str:
    """Markdown block shared by REPORT.md and README.md (single source: results.json)."""
    runs = res["runs"]
    lat, lr = runs["latency"], runs["line_rate"]
    lp, lm = lat["latency_pkt_cycles"], lat["latency_msg_cycles"]
    L = []
    L.append(f"Generated by `ticktotrade report` from `results/results.json` ({res['generated_utc']}). "
             "Clock frequencies are **assumptions** used to convert cycles to ns; no place & route "
             "or timing analysis was run.")
    L.append("")
    L.append("**Tick-to-trade latency** (realistic run: "
             f"{lat['golden_counters']['msgs']:,} ITCH messages, "
             f"{lat['golden_counters']['pkts']:,} MoldUDP64 packets, random idle cycles on the bus; "
             f"{lat['orders_compared']} orders fired)")
    L.append("")
    hdr = "| metric | cycles min / median / max | " + " | ".join(
        f"ns @ {k.split(' (')[0]}" for k in CLOCKS_MHZ) + " |"
    L.append(hdr)
    L.append("|---|---|" + "---|" * len(CLOCKS_MHZ))
    for label, st in (("packet first byte in -> order first byte out", lp),
                      ("triggering msg last byte in -> order first byte out", lm)):
        cells = " | ".join(f"{_ns(st['min'], f)} / {_ns(st['median'], f)} / {_ns(st['max'], f)}"
                           for f in CLOCKS_MHZ.values())
        L.append(f"| {label} | {st['min']} / {st['median']} / {st['max']} | {cells} |")
    L.append("")
    L.append("**Throughput** (bus is 8 bytes/cycle; no valid gaps)")
    L.append("")
    L.append("| run | msgs / cycle | book msgs / cycle | bus util | input FIFO max | orders | msg->order latency (cycles) min/med/max |")
    L.append("|---|---|---|---|---|---|---|")
    for name, desc in (("line_rate", "synthetic flow, 100% tracked symbol, 40 msgs/pkt"),
                       ("parser_worst", "back-to-back 12-byte messages"),
                       ("book_worst", "min-size deletes + replace chains, all tracked")):
        r = runs[name]
        t = r["throughput"]
        st = r["latency_msg_cycles"]
        stx = f"{st['min']} / {st['median']} / {st['max']}" if st else "n/a (no orders)"
        L.append(f"| {name}: {desc} | {t['msgs_per_cycle']} | {t['book_msgs_per_cycle']} | "
                 f"{t['bus_utilization']} | {t['fifo_max_occupancy']} | {r['orders_compared']} | {stx} |")
    L.append("")
    f0 = list(CLOCKS_MHZ.items())
    t = runs["parser_worst"]["throughput"]
    L.append("Worst-case parser rate at the assumed clocks: "
             + ", ".join(f"{t['msgs_per_cycle'] * f:.0f} M msg/s @ {k.split(' (')[0]}" for k, f in f0)
             + ". Input FIFO overflow count and order-queue overflow count were 0 in every run "
             "(checked by the harness).")
    L.append("")
    reg = res["regression"]
    rs = reg["random_small_table"]
    L.append("**Verification** (all comparisons bit-exact: every book snapshot of both sides "
             "after every book message, every OUCH byte, final order table, all counters, and "
             "RTL latency counters vs. cycle numbers derived independently from the stimulus)")
    L.append("")
    tot_snap = sum(r["snapshots_compared"] for r in runs.values()) + sum(x["snapshots"] for x in rs)
    tot_ord = sum(r["orders_compared"] for r in runs.values()) + sum(x["orders"] for x in rs)
    L.append(f"- measurement runs: {sum(r['pass'] for r in runs.values())}/{len(runs)} pass; "
             f"small-table regression (64-entry table, depth 4): {sum(x['pass'] for x in rs)}/{len(rs)} "
             f"seeds pass ({sum(x['collisions'] for x in rs)} collisions, "
             f"{sum(x['evictions'] for x in rs)} level evictions, {sum(x['drops'] for x in rs)} "
             f"level drops, {sum(x['misses'] for x in rs)} unknown-order misses exercised)")
    L.append(f"- directed edge cases: {'pass' if reg['directed_edge_cases']['pass'] else 'FAIL'}; "
             f"adversarial parser framing: {sum(not x['errors'] for x in reg['parser_adversarial'])}"
             f"/{len(reg['parser_adversarial'])} pass")
    L.append(f"- {tot_snap:,} book snapshots and {tot_ord:,} OUCH orders compared in total")
    fid = lat["fidelity"]["bbo_match_frac"]
    L.append(f"- hardware book (4096-entry direct-mapped table, 8 levels/side) matched an unbounded "
             f"reference book's best bid/offer after {fid * 100:.2f}% of updates in the latency run")
    if "mutation" in res:
        mu = res["mutation"]
        L.append(f"- mutation smoke test: {mu['killed']}/{mu['mutants']} injected RTL bugs detected")
    if "synth" in res:
        s = res["synth"]["total"]
        L.append("")
        L.append(f"**Resources** ({res['synth']['tool'].split(' (')[0]}, `synth_xilinx -family xc7`, "
                 "pre-place-and-route estimate, default parameters)")
        L.append("")
        L.append("| LUT | FF | CARRY4 | MUXF7/F8 | LUTRAM (RAM32M) | BRAM36 |")
        L.append("|---|---|---|---|---|---|")
        L.append(f"| {s['LUT']:,} | {s['FF']:,} | {s['CARRY4']} | {s['MUXF7']}/{s['MUXF8']} | "
                 f"{s['LUTRAM_RAM32M']} | {s['BRAM_RAMB36E1']} |")
        pm = res["synth"]["per_module_self"]
        L.append("")
        L.append("Per module (self, LUT/FF): " + ", ".join(
            f"{k} {v['LUT']}/{v['FF']}" for k, v in sorted(pm.items(), key=lambda kv: -kv[1]["LUT"])))
    return "\n".join(L)


def write_report(res: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "results.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    lat = res["runs"]["latency"]
    md = ["# ticktotrade measurement report", "", results_table(res), "",
          "## Latency histogram (msg last byte -> order first byte, latency run)", "",
          "| cycles | orders |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in lat["latency_msg_hist"].items()]
    md += ["", "Latency above the 4-cycle floor comes from (a) Replace messages (one extra "
           "engine cycle), (b) the input FIFO when a book message arrives while the engine is "
           "busy, (c) the order queue when an order fires while a previous 7-beat OUCH message "
           "is still being sent.", "",
           "## Message mix (latency run)", "", "| type | count |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in lat["message_mix"].items()]
    md += ["", "## Counters (latency run, golden == RTL)", "", "```",
           json.dumps(lat["golden_counters"], indent=1), "```", ""]
    if "mutation" in res:
        md += ["## Mutation smoke test", "", "| file | injected bug | result | first failing check |",
               "|---|---|---|---|"]
        for m in res["mutation"]["results"]:
            md.append(f"| {m['file']} | {m['desc']} | {m['status']} | "
                      f"{(m.get('by') or '').split(':')[0]} |")
        md.append("")
    md += ["## Assumptions and caveats", "",
           "- Simulation only (Icarus Verilog). Clock rates are assumed, not achieved: no "
           "place & route, no static timing analysis.",
           "- Latency is measured from the first byte of the MoldUDP64 payload on the RTL input "
           "bus. A real NIC path adds MAC/PCS (and Ethernet/IP/UDP header) latency on ingress and "
           "a TCP/SoupBinTCP stack + MAC/PCS on egress, which are not modelled.",
           "- Market data is synthetic. The strategy is a toy.", ""]
    (RESULTS_DIR / "REPORT.md").write_text("\n".join(md))
    if README.exists():
        txt = README.read_text()
        if BEGIN in txt and END in txt:
            a, rest = txt.split(BEGIN, 1)
            _, b = rest.split(END, 1)
            README.write_text(a + BEGIN + "\n" + results_table(res) + "\n" + END + b)


def main(skip_synth=False, skip_mutation=False, quick=False) -> int:
    if not rtl.have_iverilog():
        print("iverilog not found")
        return 2
    res = run_suite(skip_synth, skip_mutation, quick)
    write_report(res)
    ok = (all(r["pass"] for r in res["runs"].values())
          and all(x["pass"] for x in res["regression"]["random_small_table"])
          and res["regression"]["directed_edge_cases"]["pass"]
          and all(not x["errors"] for x in res["regression"]["parser_adversarial"]))
    print(f"wrote {RESULTS_DIR / 'results.json'} and {RESULTS_DIR / 'REPORT.md'}")
    print(results_table(res))
    return 0 if ok else 1
