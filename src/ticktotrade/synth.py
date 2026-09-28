"""Optional yosys synthesis for resource estimates (no place & route, no timing)."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from .rtl import BUILD_DIR, RTL_DIR, RTL_FILES


def have_yosys() -> bool:
    return shutil.which("yosys") is not None


def _yosys_version() -> str:
    r = subprocess.run(["yosys", "-V"], capture_output=True, text=True)
    return r.stdout.strip()


def parse_stat(text: str) -> dict:
    """Parse `stat` output into {module: {cell: count}} (plus '__total__')."""
    mods: dict[str, dict[str, int]] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^=== (.+) ===$", line.strip())
        if m:
            cur = m.group(1)
            mods[cur] = {}
            continue
        if line.strip() == "=== design hierarchy ===":
            cur = "__total__"
            mods[cur] = {}
            continue
        m = re.match(r"^\s+([A-Za-z0-9_$\\.]+)\s+(\d+)$", line)
        if m and cur is not None:
            mods[cur][m.group(1)] = int(m.group(2))
    if "design hierarchy" in mods:
        mods["__total__"] = mods.pop("design hierarchy")
    return mods


def summarize(cells: dict[str, int]) -> dict:
    luts = sum(v for k, v in cells.items() if re.fullmatch(r"LUT\d", k))
    ffs = sum(v for k, v in cells.items() if re.fullmatch(r"FD[A-Z]{2}", k))
    return {"LUT": luts, "FF": ffs, "CARRY4": cells.get("CARRY4", 0),
            "MUXF7": cells.get("MUXF7", 0), "MUXF8": cells.get("MUXF8", 0),
            "LUTRAM_RAM32M": cells.get("RAM32M", 0),
            "BRAM_RAMB36E1": cells.get("RAMB36E1", 0), "BRAM_RAMB18E1": cells.get("RAMB18E1", 0)}


def run_synth(out_dir: Path, order_bits: int = 12, depth: int = 8) -> dict:
    """synth_xilinx -family xc7 (7-series cell library, e.g. Kintex-7)."""
    work = BUILD_DIR / "synth"
    work.mkdir(parents=True, exist_ok=True)
    stat_path = work / "xc7_stat.txt"
    files = " ".join(str(RTL_DIR / f) for f in RTL_FILES)
    script = (f"read_verilog -sv {files}; "
              f"chparam -set ORDER_BITS {order_bits} -set DEPTH {depth} t2t_top; "
              f"synth_xilinx -family xc7 -top t2t_top -noiopad; tee -q -o {stat_path} stat")
    t0 = time.time()
    r = subprocess.run(["yosys", "-q", "-l", str(work / "xc7.log"), "-p", script],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"yosys failed: {r.stderr[-2000:]}")
    text = stat_path.read_text()
    mods = parse_stat(text)
    total = summarize(mods.get("__total__", {}))
    per_module = {}
    for name, cells in mods.items():
        if name == "__total__":
            continue
        parts = [x for x in name.split("\\") if x and not x.startswith("$") and "=" not in x]
        short = parts[0] if parts else name
        s = summarize(cells)
        if s["LUT"] or s["FF"]:
            per_module.setdefault(short, {"LUT": 0, "FF": 0})
            per_module[short]["LUT"] += s["LUT"]
            per_module[short]["FF"] += s["FF"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "synth_xc7_stat.txt").write_text(text)
    return {"tool": _yosys_version(), "flow": "synth_xilinx -family xc7 -noiopad (no P&R, no timing)",
            "params": {"ORDER_BITS": order_bits, "DEPTH": depth},
            "total": total, "per_module_self": per_module,
            "seconds": round(time.time() - t0, 1)}
