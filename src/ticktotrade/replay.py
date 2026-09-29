"""Replay real NASDAQ TotalView-ITCH 5.0 bytes through the golden model and the RTL.

Real ITCH files (the NASDAQ historical samples and excerpts cut from them)
are a plain stream of ``Length(2, BE) Message`` records, *not* MoldUDP64
packets.  :func:`frame_mold` rebuilds packets deterministically so the
parser sees realistic framing:

* messages stay in file order and get consecutive Mold sequence numbers,
  starting at 1, session ``"REPLAY    "``;
* a packet is closed before a message whose ITCH timestamp is more than
  ``batch_ns`` (default 1000 ns) after the packet's first message, or that
  would push the Mold payload (20-byte header + 2-byte prefixes + messages)
  past ``max_payload`` (default 1400 bytes, fits a 1500-byte MTU with
  Ethernet/IP/UDP headers);
* no heartbeats, no gaps, no retransmissions.

On the 64-bit bus the packets are sent back to back (``gap_prob=0``,
``ipg_max=0``): wall-clock time between packets is compressed away, so this
is a line-rate *stress* replay of real message content, not a timed replay.

The RTL tracks one stock locate per run, so every tracked symbol is a
separate run over the full (all-symbol) message stream.
"""
from __future__ import annotations

import hashlib
import random
import statistics
import struct
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import itch, mold, rtl, sim
from .book import HASH_LOW_BITS, HASH_XOR, HwBook, RefBook, event_from_msg
from .strategy import StrategyConfig

DATA_DIR = rtl.PROJECT_ROOT / "data" / "real"
SESSION = b"REPLAY    "


@dataclass(frozen=True)
class Source:
    """A real ITCH file reachable from here, pinned by commit and sha256."""
    name: str
    file: str
    sha256: str
    size: int
    repo: str
    commit: str
    path: str
    venue: str
    trading_day: str
    cut: str
    symbols: tuple = ()

    @property
    def url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.commit}/{self.path}"

    @property
    def local(self) -> Path:
        return DATA_DIR / self.file


# emi.nasdaq.com (the official sample files) is blocked by this environment's
# egress policy, so these are excerpts of those files that other projects
# committed to GitHub.  None of them are redistributed by this repo.
SOURCES = {
    "psx200k": Source(
        "psx200k", "psx200k.bin",
        "8e0f88a0a134b25717bf4dbcaff8be8d1539601c1ee98ded6c007e9b03f962a9", 5996291,
        "boyquann/nasdaq-itch50-feed-handler", "90d13cb55d67954c635e61266a01080206ae93ed",
        "tests/fixture/psx200k.bin", "Nasdaq PSX TotalView-ITCH 5.0", "2019-12-30",
        "literal prefix: the first 200,000 messages of 20191230.PSX_ITCH_50 (all symbols, "
        "unfiltered), 03:06-09:27 ET", ("QQQ", "SPY", "IWM")),
    "tv_midday": Source(
        "tv_midday", "itch50_20191230_midday.itch.gz",
        "6f164b22abb2e35fee1ab68c3e572cf9311ffb6107adf8fdf7a3fffb79aaa97c", 1602440,
        "AarinB1/limitbook", "9f01d3eacd0ae4d8e159feed828ef93b7189e8de",
        "tests/fixtures/itch50_20191230_midday.itch.gz", "Nasdaq TotalView-ITCH 5.0",
        "2019-12-30",
        "symbol-filtered cut of 12302019.NASDAQ_ITCH50: system events + AAPL/SPY/TSLA "
        "directory state from the start of day, order flow for orders added 12:00-12:20 ET",
        ("SPY", "AAPL", "TSLA")),
    "tv_premarket": Source(
        "tv_premarket", "itch50_20191230.itch.gz",
        "7bd62c758057003046195ec221ac5239374e0dca16cb471c56d270c7d20378b7", 1023282,
        "AarinB1/limitbook", "9f01d3eacd0ae4d8e159feed828ef93b7189e8de",
        "tests/fixtures/itch50_20191230.itch.gz", "Nasdaq TotalView-ITCH 5.0", "2019-12-30",
        "symbol-filtered cut of the first ~1.4M messages of 12302019.NASDAQ_ITCH50: system "
        "events + the complete stream of 13 symbols, 03:04-05:28 ET", ("NVS", "ASML", "BP")),
    "aapl_20200130": Source(
        "aapl_20200130", "locate-13-10k.bin",
        "5d407a266e807e75aa8f6d2cd7427d0eb92183023c0f92fbbf9a0e09eb86a860", 307643,
        "runk/itch", "b29c2f81f9847d63861954bd038ac28719356f12", "test/locate-13-10k.bin",
        "Nasdaq TotalView-ITCH 5.0", "2020-01-30",
        "the first 10,000 messages of locate 13 (AAPL) in 01302020.NASDAQ_ITCH50, "
        "03:07-07:59 ET", ("AAPL",)),
}


# ---------------------------------------------------------------- data
def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fetch(src: Source, timeout: float = 120.0) -> Path:
    """Download a pinned source into data/real/ (gitignored) and verify sha256."""
    dst = src.local
    if dst.exists() and sha256_file(dst) == src.sha256:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    for attempt in range(3):              # proxies occasionally drop a TLS session
        try:
            with urllib.request.urlopen(src.url, timeout=timeout) as r, open(tmp, "wb") as f:
                while b := r.read(1 << 20):
                    f.write(b)
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2)
    got = sha256_file(tmp)
    if got != src.sha256:
        tmp.unlink()
        raise ValueError(f"{src.name}: sha256 {got} != pinned {src.sha256}")
    tmp.replace(dst)
    return dst


def load_messages(path, limit: int | None = None) -> list[bytes]:
    return list(itch.read_itch_file(path, limit))


def stock_directory(msgs) -> dict[int, str]:
    """locate -> symbol from the 'R' Stock Directory messages."""
    out = {}
    for m in msgs:
        if m[0] == ord("R") and len(m) == itch.LENGTHS["R"]:
            out[(m[1] << 8) | m[2]] = m[11:19].decode("ascii").strip()
    return out


def resolve_symbols(directory: dict[int, str], symbols) -> list[tuple[int, str]]:
    by_sym = {s: loc for loc, s in directory.items()}
    out = []
    for s in symbols:
        if s.isdigit():
            out.append((int(s), directory.get(int(s), s)))
        elif s in by_sym:
            out.append((by_sym[s], s))
        else:
            raise KeyError(f"symbol {s!r} has no 'R' stock directory message in this file")
    return out


def top_locates(msgs, k: int = 3) -> list[int]:
    c = Counter(((m[1] << 8) | m[2]) for m in msgs if chr(m[0]) in itch.BOOK_TYPES)
    return [loc for loc, _ in c.most_common(k)]


def _ts(m: bytes) -> int:
    return int.from_bytes(m[5:11], "big") if len(m) >= 11 else 0


def frame_mold(msgs: list[bytes], max_payload: int = 1400, batch_ns: int = 1000,
               session: bytes = SESSION) -> list[bytes]:
    """Deterministic MoldUDP64 framing of a raw ITCH stream (see module doc)."""
    pkts, group, size, seq, t0 = [], [], mold.HEADER_LEN, 1, 0
    for m in msgs:
        need = 2 + len(m)
        if group and (size + need > max_payload or _ts(m) - t0 > batch_ns):
            pkts.append(mold.encode_packet(session, seq, group))
            seq += len(group)
            group, size = [], mold.HEADER_LEN
        if not group:
            t0 = _ts(m)
        group.append(m)
        size += need
    if group:
        pkts.append(mold.encode_packet(session, seq, group))
    return pkts


def _stats(xs):
    if not xs:
        return None
    return {"min": min(xs), "median": statistics.median(xs), "max": max(xs),
            "mean": round(statistics.fmean(xs), 3), "n": len(xs)}


def stream_stats(msgs: list[bytes], pkts: list[bytes]) -> dict:
    kinds = Counter(chr(m[0]) for m in msgs)
    wire = [len(m) + 2 for m in msgs]
    per_pkt = [struct.unpack_from(">H", p, 18)[0] for p in pkts]
    bad = sum(1 for m in msgs if itch.LENGTHS.get(chr(m[0])) != len(m))
    ts = [_ts(m) for m in msgs]
    total_payload = sum(len(p) for p in pkts)
    beats = sum((len(p) + 7) // 8 for p in pkts)
    return {
        "messages": len(msgs),
        "message_mix": dict(kinds.most_common()),
        "message_mix_frac": {k: round(v / len(msgs), 4) for k, v in kinds.most_common()},
        "book_message_frac": round(sum(v for k, v in kinds.items() if k in itch.BOOK_TYPES)
                                   / len(msgs), 4),
        "bad_length": bad,
        "timestamps_monotonic": all(a <= b for a, b in zip(ts, ts[1:])),
        "first_ts": _hms(ts[0]), "last_ts": _hms(ts[-1]),
        "system_events": [(chr(m[11]), _hms(_ts(m))) for m in msgs if m[0] == ord("S")],
        "stock_directory_entries": sum(1 for m in msgs if m[0] == ord("R")),
        "wire_bytes_per_msg": _stats(wire),
        "packets": len(pkts), "msgs_per_packet": _stats(per_pkt),
        "mold_payload_bytes": total_payload, "bus_beats": beats,
        # the parser consumes one beat per cycle and never stalls, so on a
        # gapless bus its message rate is set by the bytes per message alone
        "line_rate_msgs_per_cycle": round(len(msgs) / beats, 4),
    }


def _hms(ns: int) -> str:
    s = ns / 1e9
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


# ---------------------------------------------------------------- book analysis
def book_study(msgs: list[bytes], locate: int, order_bits: int = 12, depth: int = 8,
               ref_hash: int = HASH_XOR) -> dict:
    """Finite hardware book vs unbounded reference book on one locate."""
    hw, ref = HwBook(order_bits, depth, ref_hash), RefBook()
    adds = bbo = bbo_px = n = 0
    max_live = max_lv_bid = max_lv_ask = 0
    max_span = 0
    for i, m in enumerate(msgs):
        ev = event_from_msg(i, m, locate)
        if ev is None:
            continue
        n += 1
        adds += int(ev.kind == 1 or ev.kind == 4)
        hw.apply(ev)
        ref.apply(ev)
        hb, ha, rb, ra = hw.top(0), hw.top(1), ref.top(0), ref.top(1)
        bbo += int(hb == rb and ha == ra)
        bbo_px += int((hb and hb[0]) == (rb and rb[0]) and (ha and ha[0]) == (ra and ra[0]))
        live = len(ref.orders)
        if live > max_live:
            max_live = live
        if live and n % 64 == 0:
            span = max(ref.orders) - min(ref.orders)
            max_span = max(max_span, span)
        max_lv_bid = max(max_lv_bid, len(ref.levels[0]))
        max_lv_ask = max(max_lv_ask, len(ref.levels[1]))
    c = hw.c
    return {
        "order_bits": order_bits, "depth": depth, "ref_hash": ref_hash,
        "book_msgs": n, "adds": adds,
        "collisions": c.collisions, "misses": c.misses, "evictions": c.evictions,
        "drops": c.drops,
        "collision_rate": round(c.collisions / max(1, adds), 5),
        "miss_rate": round(c.misses / max(1, n), 5),
        "eviction_rate": round(c.evictions / max(1, adds), 5),
        "drop_rate": round(c.drops / max(1, adds), 5),
        "bbo_match_frac": round(bbo / max(1, n), 5),
        "bbo_price_match_frac": round(bbo_px / max(1, n), 5),
        "ref_max_live_orders": max_live, "ref_max_levels_bid": max_lv_bid,
        "ref_max_levels_ask": max_lv_ask, "ref_max_live_ref_span_sampled": max_span,
        "hw_live_orders_end": len(hw.table), "ref_live_orders_end": len(ref.orders),
    }


# (order_bits, depth, ref_hash) points evaluated with the golden model only
SWEEP = [(12, 8, HASH_LOW_BITS), (10, 8, HASH_XOR), (12, 8, HASH_XOR), (14, 8, HASH_XOR),
         (16, 8, HASH_XOR), (20, 8, HASH_XOR), (12, 4, HASH_XOR), (12, 16, HASH_XOR),
         (12, 32, HASH_XOR), (16, 32, HASH_XOR)]


def authenticity(msgs: list[bytes]) -> dict:
    """Cheap structural checks that the bytes look like a real ITCH 5.0 feed."""
    adds = [int.from_bytes(m[11:19], "big") for m in msgs if m[0] in b"AF"]
    first_s = next((chr(m[11]) for m in msgs if m[0] == ord("S")), None)
    return {
        "all_lengths_match_spec": all(itch.LENGTHS.get(chr(m[0])) == len(m) for m in msgs),
        "timestamps_monotonic": all(_ts(a) <= _ts(b) for a, b in zip(msgs, msgs[1:])),
        "first_system_event": first_s,
        "stock_directory_entries": sum(1 for m in msgs if m[0] == ord("R")),
        "add_refs_increasing": all(a < b for a, b in zip(adds, adds[1:])),
        "add_ref_mod4_per_locate": _mod4(msgs),
    }


def _mod4(msgs) -> dict:
    """{symbol-locate: sorted set of ref % 4} for the 3 busiest locates."""
    per: dict[int, set] = {}
    for m in msgs:
        if m[0] in b"AF":
            per.setdefault((m[1] << 8) | m[2], set()).add(int.from_bytes(m[11:19], "big") & 3)
    top = top_locates([m for m in msgs if m[0] in b"AF"], 3)
    return {str(loc): sorted(per[loc]) for loc in top}


# ---------------------------------------------------------------- one file
def replay_file(path, symbols=None, rtl_messages: int | None = None, order_bits: int = 12,
                depth: int = 8, ref_hash: int = HASH_XOR, sweep: bool = True,
                run_rtl: bool = True, rtl_legacy_symbols=(), seed: int = 1,
                strategy: dict | None = None, build_tag: str = "", log=print) -> dict:
    """Golden model over the whole file, RTL over the first ``rtl_messages``
    (default: all).  ``rtl_legacy_symbols`` get a second RTL run built with
    the old low-bit order-table index (REF_HASH=0) for a before/after view."""
    from . import golden
    t0 = time.time()
    path = Path(path)
    msgs = load_messages(path)
    if not msgs:
        raise ValueError(f"{path}: no ITCH messages")
    directory = stock_directory(msgs)
    if symbols:
        targets = resolve_symbols(directory, symbols)
    else:
        targets = [(loc, directory.get(loc, str(loc))) for loc in top_locates(msgs)]
    pkts = frame_mold(msgs)
    out = {
        "file": path.name, "file_bytes": path.stat().st_size, "sha256": sha256_file(path),
        "framing": {"max_payload": 1400, "batch_ns": 1000, "session": SESSION.decode(),
                    "bus": "back-to-back packets, no idle cycles (gap_prob=0, ipg_max=0)"},
        "params": {"order_bits": order_bits, "depth": depth, "ref_hash": ref_hash},
        "stream": stream_stats(msgs, pkts), "authenticity": authenticity(msgs), "symbols": {},
    }
    log(f"{path.name}: {len(msgs)} msgs, {len(pkts)} packets, "
        f"{len(directory)} stock directory entries")
    # RTL window: the smallest packet prefix holding >= rtl_messages messages
    win_pkts, nwin = pkts, len(msgs)
    if rtl_messages is not None and rtl_messages < len(msgs):
        acc = 0
        for k, p in enumerate(pkts):
            acc += struct.unpack_from(">H", p, 18)[0]
            if acc >= rtl_messages:
                win_pkts, nwin = pkts[:k + 1], acc
                break
    for loc, sym in targets:
        scfg = StrategyConfig(locate=loc, stock=sym[:8], **(strategy or {}))
        r: dict = {"locate": loc, "symbol": sym,
                   "strategy": {"imb_shift": scfg.imb_shift, "max_spread": scfg.max_spread,
                                "max_qty": scfg.max_qty}}
        r["book"] = book_study(msgs, loc, order_bits, depth, ref_hash)
        if sweep:
            r["sweep"] = [book_study(msgs, loc, ob, d, h) for ob, d, h in SWEEP]
        g = golden.run_golden(pkts, scfg, order_bits, depth, ref_hash)
        r["golden_full"] = {"counters": g.counters, "fidelity": g.fidelity,
                            "orders": len(g.orders),
                            "fire_rate_per_book_msg":
                                round(len(g.orders) / max(1, g.counters["relevant"]), 5)}
        if run_rtl and rtl.have_iverilog():
            modes = [ref_hash] + ([HASH_LOW_BITS] if sym in rtl_legacy_symbols
                                  and ref_hash != HASH_LOW_BITS else [])
            for h in modes:
                key = "rtl" if h == ref_hash else "rtl_low_bit_index"
                cfg = sim.SimConfig(seed=seed, order_bits=order_bits, depth=depth,
                                    gap_prob=0.0, ipg_max=0, ref_hash=h, strategy=scfg)
                wd = rtl.BUILD_DIR / f"replay{build_tag}_{path.name.split('.')[0]}_{sym}_h{h}"
                log(f"  {sym} (locate {loc}, REF_HASH={h}): RTL over {nwin} msgs / "
                    f"{len(win_pkts)} packets ...")
                rr = sim.run_packets(win_pkts, cfg, wd)
                x = {k: rr[k] for k in ("pass", "errors", "snapshots_compared",
                                        "orders_compared", "latency_pkt_cycles",
                                        "latency_msg_cycles", "latency_msg_hist",
                                        "throughput", "wall_seconds", "golden_counters")}
                x["messages"] = nwin
                x["ref_hash"] = h
                x["latency_pkt_hist"] = _hist_pkt(wd)
                r[key] = x
                log(f"    {'PASS' if rr['pass'] else 'FAIL'}: {rr['snapshots_compared']} "
                    f"snapshots, {rr['orders_compared']} orders bit-exact, "
                    f"rtl {rr['wall_seconds']['rtl_sim']} s")
                for e in rr["errors"][:5]:
                    log("    ERROR:", e)
        out["symbols"][sym] = r
    out["wall_seconds"] = round(time.time() - t0, 1)
    return out


def _hist_pkt(workdir: Path) -> dict:
    lat = [int(ln.split()[2]) for ln in (workdir / "rtl_out.txt").read_text().splitlines()
           if ln.startswith("O ")]
    return {str(k): lat.count(k) for k in sorted(set(lat))}


# ---------------------------------------------------------------- suite
LEGACY_RTL = {"tv_midday": ("AAPL",)}


def ensure_source(src: Source, allow_fetch: bool = True, log=print) -> Path:
    p = src.local
    if not p.exists():
        if not allow_fetch:
            raise FileNotFoundError(f"{p} (run `ticktotrade fetch-real`)")
        log(f"fetching {src.url}")
        fetch(src)
    if sha256_file(p) != src.sha256:
        raise ValueError(f"{p}: sha256 does not match the pinned {src.sha256}")
    return p


def cross_check_directories(paths: dict) -> dict:
    """Same-day files from different uploaders and venues must agree on the
    stock directory: locate -> symbol and every static 'R' field."""
    dirs = {}
    for name, p in paths.items():
        dirs[name] = {(m[1] << 8) | m[2]: m[11:] for m in load_messages(p) if m[0] == ord("R")}
    out = {}
    names = sorted(dirs)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = set(dirs[a]) & set(dirs[b])
            out[f"{a}~{b}"] = {"common_locates": len(common),
                               "identical_R_fields": sum(dirs[a][k] == dirs[b][k] for k in common)}
    return out


def directory_cross_checks(names, paths) -> dict:
    """Cross-check the stock directories of files from the same trading day."""
    out = {}
    days: dict[str, list] = {}
    for n in names:
        days.setdefault(SOURCES[n].trading_day, []).append(n)
    for group in days.values():
        if len(group) > 1:
            out.update(cross_check_directories({n: paths[n] for n in group}))
    return out


def run_suite(sources=None, rtl_messages: int | None = None, allow_fetch: bool = True,
              log=print) -> dict:
    res = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sources": {}}
    names = list(sources or SOURCES)
    paths = {n: ensure_source(SOURCES[n], allow_fetch, log) for n in names}
    for name in names:
        src = SOURCES[name]
        r = replay_file(paths[name], src.symbols, rtl_messages,
                        rtl_legacy_symbols=LEGACY_RTL.get(name, ()), log=log)
        r["source"] = {"url": src.url, "repo": src.repo, "commit": src.commit,
                       "path": src.path, "venue": src.venue, "trading_day": src.trading_day,
                       "cut": src.cut}
        res["sources"][name] = r
    res["directory_cross_check"] = directory_cross_checks(names, paths)
    rtl_runs = [x for r in res["sources"].values() for s in r["symbols"].values()
                for k, x in s.items() if k.startswith("rtl")]
    res["totals"] = {
        "distinct_real_messages": sum(r["stream"]["messages"] for r in res["sources"].values()),
        "real_bytes": sum(r["file_bytes"] for r in res["sources"].values()),
        "rtl_runs": len(rtl_runs),
        "rtl_messages_simulated": sum(x["messages"] for x in rtl_runs),
        "rtl_snapshots_compared": sum(x["snapshots_compared"] for x in rtl_runs),
        "rtl_orders_compared": sum(x["orders_compared"] for x in rtl_runs),
        "rtl_sim_seconds": round(sum(x["wall_seconds"]["rtl_sim"] for x in rtl_runs), 1),
    }
    res["all_pass"] = bool(rtl_runs) and all(x["pass"] for x in rtl_runs)
    return res


# ---------------------------------------------------------------- README block
BEGIN, END = "<!-- REAL_ITCH:BEGIN -->", "<!-- REAL_ITCH:END -->"


def _pct(x) -> str:
    return f"{100 * x:.1f}%"


def _mmm(d) -> str:
    return f"{d['min']} / {d['median']:g} / {d['max']}" if d else "n/a"


def real_table(res: dict) -> str:
    """Markdown block for README.md, generated only from results/real_itch.json."""
    t = res["totals"]
    L = [f"Generated by `ticktotrade real-report` from `results/real_itch.json` ({res['generated']}). "
         f"{t['distinct_real_messages']:,} distinct real ITCH messages ({t['real_bytes']:,} bytes) "
         f"from {len(res['sources'])} files; {t['rtl_runs']} RTL runs simulated {t['rtl_messages_simulated']:,} "
         f"messages in total ({t['rtl_sim_seconds']:,} s of Icarus time), comparing "
         f"{t['rtl_snapshots_compared']:,} book snapshots and {t['rtl_orders_compared']:,} OUCH "
         f"orders: **{'all bit-exact' if res['all_pass'] else 'MISMATCHES, see json'}**.", ""]
    L += ["**Sources** (NASDAQ historical sample files, via excerpts committed to GitHub; "
          "not redistributed here, `ticktotrade fetch-real` downloads them pinned by commit and sha256)", "",
          "| name | venue, trading day | what the excerpt is | messages | bytes | sha256 |",
          "|---|---|---|---|---|---|"]
    for name, r in res["sources"].items():
        s = r["source"]
        L.append(f"| `{name}` | {s['venue']}, {s['trading_day']} | {s['cut']}; "
                 f"[{s['repo']}@{s['commit'][:7]}]({s['url']}) | {r['stream']['messages']:,} | "
                 f"{r['file_bytes']:,} | `{r['sha256']}` |")
    L += ["", "**Stream** (after MoldUDP64 framing: <= 1 us of ITCH time and <= 1400 B per packet, "
          "sent back to back on the 8 B/cycle bus)", "",
          "| name | time span (ET) | message mix | wire B/msg mean (min-max) | msgs/packet mean (max) | "
          "parser msgs/cycle | M msg/s @ 250 MHz |", "|---|---|---|---|---|---|---|"]
    for name, r in res["sources"].items():
        st = r["stream"]
        mix = ", ".join(f"{k} {_pct(v)}" for k, v in list(st["message_mix_frac"].items())[:6])
        w, pp = st["wire_bytes_per_msg"], st["msgs_per_packet"]
        L.append(f"| `{name}` | {st['first_ts'][:8]}-{st['last_ts'][:8]} | {mix} | "
                 f"{w['mean']:.1f} ({w['min']}-{w['max']}) | {pp['mean']:.2f} ({pp['max']}) | "
                 f"{st['line_rate_msgs_per_cycle']} | {st['line_rate_msgs_per_cycle'] * 250:.1f} |")
    L += ["", "**RTL vs golden, per tracked symbol** (whole file through the RTL; default build: "
          "4096-entry table with the XOR index hash, 8 levels/side)", "",
          "| file / symbol (locate) | book msgs | bit-exact | snapshots / orders | "
          "msg->order cycles min/med/max | pkt->order cycles min/med/max | FIFO max | "
          "strategy fires per 1k book msgs |", "|---|---|---|---|---|---|---|---|"]
    for name, r in res["sources"].items():
        for sym, s in r["symbols"].items():
            for key in ("rtl", "rtl_low_bit_index"):
                x = s.get(key)
                if not x:
                    continue
                tag = "" if key == "rtl" else " (REF_HASH=0)"
                L.append(f"| `{name}` {sym} ({s['locate']}){tag} | {x['golden_counters']['relevant']:,} | "
                         f"{'yes' if x['pass'] else '**NO**'} | {x['snapshots_compared']:,} / "
                         f"{x['orders_compared']:,} | {_mmm(x['latency_msg_cycles'])} | "
                         f"{_mmm(x['latency_pkt_cycles'])} | {x['throughput']['fifo_max_occupancy']} | "
                         f"{1000 * x['orders_compared'] / max(1, x['golden_counters']['relevant']):.0f} |")
    L += ["", "**Finite hardware book vs unbounded reference book** (golden model, whole file; "
          "rates per Add/Replace except misses, which are per book message)", "",
          "| file / symbol | collisions, low-bit index -> XOR hash | misses | level evictions | "
          "level drops | BBO exact (px+qty) | BBO price | ref book: max live orders / max levels bid, ask |",
          "|---|---|---|---|---|---|---|---|"]
    for name, r in res["sources"].items():
        for sym, s in r["symbols"].items():
            b = s["book"]
            lb = next(w for w in s["sweep"] if w["ref_hash"] == 0 and w["order_bits"] == 12
                      and w["depth"] == 8)
            L.append(f"| `{name}` {sym} | {_pct(lb['collision_rate'])} -> {_pct(b['collision_rate'])} | "
                     f"{_pct(b['miss_rate'])} | {_pct(b['eviction_rate'])} | {_pct(b['drop_rate'])} | "
                     f"{_pct(b['bbo_match_frac'])} | {_pct(b['bbo_price_match_frac'])} | "
                     f"{b['ref_max_live_orders']} / {b['ref_max_levels_bid']}, {b['ref_max_levels_ask']} |")
    hard = res["sources"].get("tv_midday", {}).get("symbols", {}).get("AAPL")
    if hard:
        L += ["", "**Sizing sweep on the hardest case** (`tv_midday` AAPL, golden model only)", "",
              "| table entries | levels/side | index | collisions | misses | evictions | drops | "
              "BBO exact | BBO price |", "|---|---|---|---|---|---|---|---|---|"]
        for w in hard["sweep"]:
            L.append(f"| {1 << w['order_bits']:,} | {w['depth']} | "
                     f"{'XOR hash' if w['ref_hash'] else 'low bits'} | {w['collisions']:,} | "
                     f"{w['misses']:,} | {w['evictions']:,} | {w['drops']:,} | "
                     f"{_pct(w['bbo_match_frac'])} | {_pct(w['bbo_price_match_frac'])} |")
    cc = res.get("directory_cross_check", {})
    if cc:
        L += ["", "Stock directory cross-check between files of the same trading day (the PSX "
              "and TotalView files come from different uploaders and venues; 'R' fields after "
              "the timestamp compared byte for byte): " + "; ".join(f"`{k}` {v['identical_R_fields']}/{v['common_locates']} "
                                          f"identical 'R' records" for k, v in cc.items()) + "."]
    return "\n".join(L)


def write_readme(res: dict, readme: Path | None = None) -> bool:
    readme = readme or rtl.PROJECT_ROOT / "README.md"
    txt = readme.read_text()
    if BEGIN not in txt or END not in txt:
        return False
    a, rest = txt.split(BEGIN, 1)
    _, b = rest.split(END, 1)
    readme.write_text(a + BEGIN + "\n" + real_table(res) + "\n" + END + b)
    return True
