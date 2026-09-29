"""Mutation smoke test: inject small, plausible RTL bugs and check that the
differential flow (parser check + random sim + directed edge cases) catches
each one.  Slow (~10 s per mutant); run via `ticktotrade report` or
`ticktotrade mutate`."""
from __future__ import annotations

import os
import random
import shutil
import tempfile
from pathlib import Path

MUTANTS = [
    ("itch_parser.v", "wire        region_ends = (skip <= {12'd0, n});",
     "wire        region_ends = (skip < {12'd0, n});", "region ending exactly at beat end missed"),
    ("itch_parser.v", "s = p + 4'd2;", "s = p + 4'd1;", "new body starts one lane early"),
    ("itch_parser.v", "end else if (L <= {12'd0, avail}) begin", "end else if (1'b0) begin",
     "drop the split-prefix L==7 completion path"),
    ("itch_parser.v", "localparam MINLEN = 16'd7;", "localparam MINLEN = 16'd12;",
     "wrong minimum message length"),
    ("msg_decode.v", "wire is_c = (t == \"C\") && (msg_len == 16'd36);",
     "wire is_c = 1'b0;", "ignore Order Executed With Price"),
    ("msg_decode.v", "{`MB(31), `MB(32), `MB(33), `MB(34)}", "{`MB(32), `MB(33), `MB(34), `MB(35)}",
     "replace price at wrong offset"),
    ("price_levels.v", "(mq <= op_q)", "(mq < op_q)", "level not removed when qty hits 0"),
    ("price_levels.v", "wire        ins       = op_add && !any_match && better[DEPTH-1] && (op_q != 0);",
     "wire        ins       = op_add && !any_match && better[DEPTH-1];",
     "zero-share add creates a level"),
    ("price_levels.v", "(op_px > px[i]) : (op_px < px[i])", "(op_px > px[i]) : (op_px > px[i])",
     "ask side sorted the wrong way"),
    ("book_engine.v", "wire [31:0] dec    = (c_sh < e_sh) ? c_sh : e_sh;",
     "wire [31:0] dec    = c_sh;", "no clamp on over-execution"),
    ("book_engine.v", "wire        u2_free = !e_v || (c_slot2 == c_slot);",
     "wire        u2_free = !e_v;", "same-slot replace read-before-write hazard"),
    ("book_engine.v", "slot_of[b] = (REF_HASH != 0) ? ^(r & hmask(b)) : r[b];",
     "slot_of[b] = r[b];", "order-table index ignores the reference hash"),
    ("book_engine.v", "if (!e_v) begin", "if (!hit) begin", "add overwrites colliding order"),
    ("strategy.v", "wire        sell_f = upd && sell_c && !ps && !buy_f;",
     "wire        sell_f = upd && sell_c && !buy_f;", "sell not edge-triggered"),
    ("strategy.v", "assign fire_q    = (avail < cfg_max_qty) ? avail : cfg_max_qty;",
     "assign fire_q    = avail;", "max_qty cap ignored"),
    ("ouch_tx.v", "msg[8*48 +: 8] = \"N\";", "msg[8*48 +: 8] = \"R\";", "wrong customer type"),
    ("ouch_tx.v", "wire [31:0] lm = now + 32'd1 - s_eom;", "wire [31:0] lm = now - s_eom;",
     "latency counter off by one"),
]


def _kill_checks() -> list[str]:
    from . import gen, scenarios, sim
    fails = []
    for seed, bad in ((11, 0.0), (12, 0.3)):
        e = sim.run_parser_check(sim.random_parser_packets(seed, 250, bad_rate=bad), seed, 0.15)
        if e:
            fails.append("parser: " + e[0])
            return fails
    r = sim.run_sim(sim.SimConfig(messages=3000, seed=21, order_bits=6, depth=4))
    if not r["pass"]:
        fails.append("random: " + r["errors"][0])
        return fails
    pk = gen.packetize(scenarios.edge_case_messages(), random.Random(3), 3, heartbeat_rate=0.1)
    r = sim.run_packets(pk, sim.SimConfig(seed=3, order_bits=4, depth=4, gap_prob=0.3))
    if not r["pass"]:
        fails.append("directed: " + r["errors"][0])
    return fails


def run_mutation(root: Path) -> dict:
    from . import rtl
    results = []
    orig_root = rtl.PROJECT_ROOT
    try:
        for fname, old, new, desc in MUTANTS:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                shutil.copytree(root / "rtl", td / "rtl")
                shutil.copytree(root / "tb", td / "tb")
                src = (td / "rtl" / fname).read_text()
                if old not in src:
                    results.append({"file": fname, "desc": desc, "status": "NOT_APPLIED"})
                    continue
                (td / "rtl" / fname).write_text(src.replace(old, new, 1))
                rtl.PROJECT_ROOT, rtl.RTL_DIR, rtl.TB_DIR = td, td / "rtl", td / "tb"
                rtl.BUILD_DIR = td / "build"
                try:
                    fails = _kill_checks()
                except RuntimeError as ex:       # compile / runtime failure also kills
                    fails = [f"sim error: {str(ex)[:80]}"]
                results.append({"file": fname, "desc": desc,
                                "status": "KILLED" if fails else "SURVIVED",
                                "by": fails[0][:140] if fails else None})
    finally:
        rtl.PROJECT_ROOT, rtl.RTL_DIR, rtl.TB_DIR = orig_root, orig_root / "rtl", orig_root / "tb"
        rtl.BUILD_DIR = orig_root / "build"
    killed = sum(r["status"] == "KILLED" for r in results)
    return {"mutants": len(results), "killed": killed, "results": results}


if __name__ == "__main__":  # pragma: no cover
    import json
    from .rtl import PROJECT_ROOT
    print(json.dumps(run_mutation(PROJECT_ROOT), indent=1))
    os._exit(0)
