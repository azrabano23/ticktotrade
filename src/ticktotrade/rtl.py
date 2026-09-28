"""Compile and run the Verilog testbenches with Icarus Verilog."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("TICKTOTRADE_ROOT", Path(__file__).resolve().parents[2]))
RTL_DIR = PROJECT_ROOT / "rtl"
TB_DIR = PROJECT_ROOT / "tb"
BUILD_DIR = PROJECT_ROOT / "build"

RTL_FILES = ["itch_parser.v", "msg_decode.v", "sync_fifo.v", "order_table.v",
             "price_levels.v", "book_engine.v", "strategy.v", "ouch_tx.v", "t2t_top.v"]


def have_iverilog() -> bool:
    return shutil.which("iverilog") is not None and shutil.which("vvp") is not None


def compile_tb(top: str = "tb", params: dict | None = None) -> Path:
    """Compile a testbench (cached on source contents + parameters)."""
    params = params or {}
    tb_file = TB_DIR / ("tb_top.v" if top == "tb" else f"{top}.v")
    srcs = [RTL_DIR / f for f in RTL_FILES] + [tb_file]
    h = hashlib.sha1()
    for s in srcs:
        h.update(s.read_bytes())
    h.update(repr(sorted(params.items())).encode())
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    out = BUILD_DIR / f"{top}_{h.hexdigest()[:12]}.vvp"
    if not out.exists():
        cmd = ["iverilog", "-g2012", "-Wall", "-Wno-timescale", "-o", str(out), "-s", top]
        for k, v in params.items():
            cmd += ["-P", f"{top}.{k}={v}"]
        cmd += [str(s) for s in srcs]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"iverilog failed:\n{r.stdout}\n{r.stderr}")
    return out


def run_vvp(vvp: Path, plusargs: dict) -> str:
    cmd = ["vvp", "-n", str(vvp)] + [f"+{k}={v}" for k, v in plusargs.items()]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"vvp failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout


def _split32(hexstr: str, n: int) -> list[int]:
    v = int(hexstr, 16)
    return [(v >> (32 * i)) & 0xFFFFFFFF for i in range(n)]


def _levels(vhex: str, pxhex: str, qhex: str, depth: int) -> list[tuple[int, int]]:
    v = int(vhex, 16)
    px = _split32(pxhex, depth)
    q = _split32(qhex, depth)
    out = []
    for i in range(depth):
        if (v >> i) & 1:
            out.append((px[i], q[i]))
        else:
            break
    # contiguity check: nothing valid after the first hole
    if v >> len(out):
        out.append(("HOLE", v))
    return out


def parse_top_output(path: Path, depth: int) -> dict:
    snaps, orders, table, counters, errors = [], [], {}, {}, []
    for line in Path(path).read_text().splitlines():
        p = line.split()
        if not p:
            continue
        tag = p[0]
        if tag == "B":
            snaps.append((int(p[1]), _levels(p[2], p[3], p[4], depth),
                          _levels(p[5], p[6], p[7], depth)))
        elif tag == "O":
            raw = bytes.fromhex(p[5].rjust(98, "0"))[::-1]
            orders.append({"cycle": int(p[1]), "lat_pkt": int(p[2]), "lat_msg": int(p[3]),
                           "msg_id": int(p[4]), "bytes": raw})
        elif tag == "T":
            e = int(p[2], 16)
            table[int(p[1])] = ((e >> 65) & ((1 << 64) - 1), (e >> 64) & 1,
                                (e >> 32) & 0xFFFFFFFF, e & 0xFFFFFFFF)
        elif tag == "C":
            counters[p[1]] = [int(x) for x in p[2:]] if len(p) > 3 else int(p[2])
        else:
            errors.append(line)
    return {"snapshots": snaps, "orders": orders, "table": table, "counters": counters,
            "errors": errors}


def parse_parser_output(path: Path, cap: int = 40) -> dict:
    msgs, counters = [], {}
    for line in Path(path).read_text().splitlines():
        p = line.split()
        if p and p[0] == "P":
            raw = bytes.fromhex(p[5].rjust(2 * cap, "0"))[::-1]
            msgs.append({"msg_id": int(p[1]), "len": int(p[2]), "sof": int(p[3]),
                         "eom": int(p[4]), "bytes": raw})
        elif p and p[0] == "C":
            counters[p[1]] = int(p[2])
    return {"msgs": msgs, "counters": counters}
