# ticktotrade

An FPGA-style **tick-to-trade pipeline in synthesizable Verilog**: NASDAQ
TotalView-ITCH 5.0 market data arrives in MoldUDP64 packets on a 64-bit
streaming bus, a hardware order book is maintained on chip, a (deliberately
toy) strategy watches the top of book, and an OUCH 4.2 *Enter Order* message
goes out on a second 64-bit stream. Every cycle is verified **bit-exactly**
against a Python golden model, and latency is measured **in clock cycles** by
counters inside the RTL, then cross-checked against cycle numbers computed
independently from the stimulus.

Everything here runs in simulation (Icarus Verilog). There is no FPGA, no
timing closure, and no real exchange connectivity. See
[What is real and what is not](#what-is-real-and-what-is-not).

## Why this is hard to copy

Three skills have to work together here, and most projects only show one of them:

1. **RTL micro-architecture.** A parser that de-frames messages straddling
   arbitrary 8-byte beat boundaries in one pass per beat, without a serial
   byte loop (the first version had one, and yosys mapped it to ~22k LUT/FF/MUX cells; the
   parallel version is ~1.6k LUTs). A block-RAM order table with a read-modify-write FSM that has
   no hazards. A sorted, fixed-depth price-level array that inserts and
   removes in one cycle. A zero-bubble output serializer.
2. **Market microstructure.** ITCH add, execute, cancel, delete and replace
   semantics, order-reference handling, price-time priority in the synthetic
   flow, MoldUDP64 framing, and OUCH order entry fields (token, TIF, capacity,
   ISO, and the rest).
3. **Verification discipline.** A golden model that mirrors the hardware's
   *finite* structures exactly: direct-mapped table collisions, level
   eviction, and execution clamping. Every book snapshot is compared, not
   just the final state. There is an adversarial parser test, directed edge
   cases, a small-table regression, and a **mutation smoke test** that
   injects realistic RTL bugs and checks the flow catches them. An unbounded
   reference book measures how much the finite hardware book differs from
   the "true" book.

## Block diagram

```
             64b/cycle stream (valid/last/keep), one MoldUDP64 payload per frame
                                     |
                                     v
 +-----------------------------------------------------------------------------+
 | itch_parser     per-beat parallel de-framer: skip 20B Mold header, 2B length |
 |                 prefixes at any lane (split prefixes carried), captures first |
 |                 40 bytes of every message, tags {msg_id, pkt SOF cycle, EOM}  |
 +-----------------------------------------------------------------------------+
                                     | 1 msg/cycle max (registered)
                                     v
 +------------------+   locate == cfg_locate && type in {A,F,E,C,X,D,U} && len ok
 | msg_decode (comb)|-------------------------------------------------------+
 +------------------+                                                       |
                                                                            v
 +-----------------------------------------------------------------------------+
 | book_engine   8-deep input FIFO with bypass                                  |
 |   IDLE -> RD -> (U2)      order_table: 4096 x 130b BRAM, direct-mapped by    |
 |                           ref[11:0], full 64b ref tag, drop-new on collision |
 |                           price_levels x2: 8 sorted levels/side, 1 op/cycle  |
 +-----------------------------------------------------------------------------+
                                     | upd pulse + top of book
                                     v
 +------------------+  edge-triggered imbalance: bid_q >= ask_q<<k (buy the ask)
 | strategy (comb)  |  or ask_q >= bid_q<<k (hit the bid), spread <= max_spread
 +------------------+
                                     | fire {side, px, qty, tags}
                                     v
 +-----------------------------------------------------------------------------+
 | ouch_tx   OUCH 4.2 'O' (49B) -> 7 beats, 8-deep order queue, latency counters |
 +-----------------------------------------------------------------------------+
                                     |
                                     v
                          64b/cycle order-entry stream
```

## Quickstart

```bash
sudo apt-get install -y iverilog        # yosys optional, for resource estimates
cd ticktotrade
pip install -e '.[test]'
pytest -q                               # ~40 s; RTL tests skip without iverilog, real-data tests without network
ticktotrade sim --messages 20000 --seed 7
ticktotrade report                      # measurement suite -> results/ (+ this README's table)
ticktotrade report --skip-synth --skip-mutation   # fast version (~30 s)
ticktotrade mutate                      # mutation smoke test only
ticktotrade fetch-real                  # real ITCH excerpts -> data/real/ (gitignored, sha256 pinned)
ticktotrade replay --itch-file data/real/itch50_20191230_midday.itch.gz --symbols SPY,AAPL
ticktotrade real-report                 # all real sources -> results/real_itch.json (+ README block), ~20 min
```

`ticktotrade sim` prints PASS/FAIL, the book counters, the latency
distribution and throughput. Stimulus and RTL dumps go to `build/`, which git
ignores. `ticktotrade replay` takes any length-prefixed ITCH 5.0 file (the
NASDAQ sample format, optionally gzipped), finds each symbol's stock locate
in the `R` messages, runs the golden model over the whole file and the RTL
over the first `--rtl-messages` (default: all), and compares them bit-exactly.
No market data is committed; see [Real ITCH replay](#real-itch-replay).

## Results

Everything between the markers below is generated by `ticktotrade report`
from `results/results.json`. `tests/test_report.py` fails if this README
drifts from that file. More detail is in [results/REPORT.md](results/REPORT.md).

<!-- RESULTS:BEGIN -->
Generated by `ticktotrade report` from `results/results.json` (2026-09-29T15:47:13Z). Clock frequencies are **assumptions** used to convert cycles to ns; no place & route or timing analysis was run.

**Tick-to-trade latency** (realistic run: 30,000 ITCH messages, 12,109 MoldUDP64 packets, random idle cycles on the bus; 566 orders fired)

| metric | cycles min / median / max | ns @ 156.25 MHz | ns @ 250 MHz | ns @ 322.27 MHz |
|---|---|---|---|---|
| packet first byte in -> order first byte out | 9 / 15.0 / 31 | 57.6 / 96.0 / 198.4 | 36.0 / 60.0 / 124.0 | 27.9 / 46.5 / 96.2 |
| triggering msg last byte in -> order first byte out | 4 / 4.0 / 7 | 25.6 / 25.6 / 44.8 | 16.0 / 16.0 / 28.0 | 12.4 / 12.4 / 21.7 |

**Throughput** (bus is 8 bytes/cycle; no valid gaps)

| run | msgs / cycle | book msgs / cycle | bus util | input FIFO max | orders | msg->order latency (cycles) min/med/max |
|---|---|---|---|---|---|---|
| line_rate: synthetic flow, 100% tracked symbol, 40 msgs/pkt | 0.2415 | 0.2414 | 1.0 | 1 | 855 | 4 / 4 / 11 |
| parser_worst: back-to-back 12-byte messages | 0.5618 | 0.0 | 1.0 | 0 | 0 | n/a (no orders) |
| book_worst: min-size deletes + replace chains, all tracked | 0.2817 | 0.2817 | 1.0 | 1 | 0 | n/a (no orders) |

Worst-case parser rate at the assumed clocks: 88 M msg/s @ 156.25 MHz, 140 M msg/s @ 250 MHz, 181 M msg/s @ 322.27 MHz. Input FIFO overflow count and order-queue overflow count were 0 in every run (checked by the harness).

**Verification** (all comparisons bit-exact: every book snapshot of both sides after every book message, every OUCH byte, final order table, all counters, and RTL latency counters vs. cycle numbers derived independently from the stimulus)

- measurement runs: 4/4 pass; small-table regression (64-entry table, depth 4): 8/8 seeds pass (2429 collisions, 358 level evictions, 606 level drops, 4290 unknown-order misses exercised)
- directed edge cases: pass; adversarial parser framing: 4/4 pass
- 67,256 book snapshots and 1,912 OUCH orders compared in total
- hardware book (4096-entry direct-mapped table, 8 levels/side) matched an unbounded reference book's best bid/offer after 95.44% of updates in the latency run
- mutation smoke test: 17/17 injected RTL bugs detected

**Resources** (Yosys 0.33, `synth_xilinx -family xc7`, pre-place-and-route estimate, default parameters)

| LUT | FF | CARRY4 | MUXF7/F8 | LUTRAM (RAM32M) | BRAM36 |
|---|---|---|---|---|---|
| 7,421 | 3,203 | 660 | 583/195 | 76 | 15 |

Per module (self, LUT/FF): price_levels 3239/1040, book_engine 1602/589, itch_parser 1560/941, ouch_tx 442/573, strategy 436/2, msg_decode 107/0, sync_fifo 34/26, t2t_top 1/32
<!-- RESULTS:END -->

## Real ITCH replay

Real NASDAQ TotalView-ITCH 5.0 bytes have now been through the golden
model and the RTL, and every book snapshot and OUCH byte matched. Real data
also found a design flaw that the synthetic generator could not show. It is
fixed below.

**Where the data comes from.** The official samples at
`https://emi.nasdaq.com/ITCH/` (and nasdaqtrader.com) are blocked by this
environment's egress policy. The proxy answers the CONNECT with 403 "Host
not in allowlist", over both https and http, for the directory and the
`Nasdaq%20ITCH/` path. The data used here are therefore excerpts of those
NASDAQ sample files that other projects committed to GitHub. They were found
by scanning ~160 ITCH-related GitHub repositories for large binary blobs and
searching HuggingFace datasets (none had raw ITCH). Every file is pinned by
commit and sha256 in `src/ticktotrade/replay.py`. Candidates that turned out
to be synthetic were rejected: all symbols at locates 1-3, every system
event at 09:30:00, or tickers "AAA/AAB". A 1 GB Git LFS copy was also
rejected, because this proxy does not serve LFS objects.

**Why the data is believed to be real.**
- Every message length matches the 5.0 spec.
- Timestamps are monotonic, and the system events fall on the NASDAQ
  schedule: `O` at ~03:05, `S` at 04:00 (08:00 on PSX), `Q` at 09:30.
- The PSX prefix carries the full 8,906-entry stock directory.
- Trade prints on 2019-12-30 fall in AAPL $289.6-291.8, SPY $321.4-323.1 and
  TSLA $418.1-433.4. That is plausible for the date, but it was not checked
  against an external price source.
- The strongest check crosses files. The PSX prefix and the TotalView
  excerpts of 2019-12-30 come from different uploaders and different NASDAQ
  venues, yet they carry byte-identical `R` records (after the timestamp)
  for all 13 locates they share. AAPL is locate 13 in both, SPY 7451, TSLA
  7992.

**Framing.** A raw sample file is a sequence of `Length(2, BE) Message`
records, not packets. `replay.frame_mold` builds MoldUDP64 packets in file
order with consecutive sequence numbers. A new packet starts when a message
is more than 1 us of ITCH time after the packet's first message, or when
the payload would exceed 1400 B. Packets go onto the bus back to back, so
wall-clock gaps are compressed away: this is a line-rate stress replay of
real content, not a timed replay. Each RTL run tracks one locate but parses
the full stream.

**Redistribution.** NASDAQ offers these files as samples, but no license
permitting redistribution was found (and the terms could not be checked
from here), so no real bytes are committed. `ticktotrade fetch-real`
downloads the pinned excerpts into the gitignored `data/real/`, and
`tests/test_real_itch.py` fetches them or skips.

**Findings.**
- *Order-table index (bug, fixed).* On NASDAQ's main book, all order
  references of one symbol share `ref % 4`, on both days examined. AAPL is
  always `4k+1` and SPY `4k`. References are also not globally monotonic,
  which fits several matching-engine partitions each numbering with stride 4.
  PSX shows no such pattern. The old `ref[11:0]` index could therefore use
  only 1,024 of 4,096 slots for any one symbol. Midday AAPL shows the effect:
  29% of adds collided while the table was on average only 8% full. The XOR
  hash index fixes this, cutting collisions 3-6x on every TotalView symbol
  (AAPL midday: 28.8% to 9.1%, close to what a uniform index gives at that
  load). The RTL was replayed bit-exactly in both builds. The stride-4
  pattern has regression tests in `tests/test_golden.py` and
  `tests/test_rtl.py`.
- *The finite book is the real limit on busy names.* Midday AAPL has up to
  740 live orders over 181 bid levels. With 8 levels per side, the top of
  book is exactly right after only 47% of updates, and has the right price
  after 72%. The sizing sweep shows that the table and the level depth
  both matter: 65k entries with 32 levels reaches 93% / 98%. On quiet
  symbols (PSX, pre-market) the hardware book matches the unbounded one
  after 91-100% of updates.
- *Latency and throughput are unchanged by real data.* Processing latency
  was 4 cycles for 95.6% of 12,794 real-data orders (maximum 9). The input
  FIFO never held more than 1 record. Real messages average ~30 wire bytes,
  so the parser runs at ~0.145 msgs/cycle at line rate (36 M msg/s at an
  assumed 250 MHz). The book engine's peak load was 0.14 book msgs/cycle,
  against a capacity of 0.33-0.5.
- *The toy strategy* fired on 0-27% of book updates, depending on the
  symbol's spread and imbalance.

The 1M-message RTL target was met only in aggregate: 1.52M messages
simulated across 11 runs. The reachable real data is 462k distinct
messages, and the busiest continuous-trading excerpt is symbol-filtered
and only 20 minutes long.

<!-- REAL_ITCH:BEGIN -->
Generated by `ticktotrade real-report` from `results/real_itch.json` (2026-09-29T15:28:37Z). 462,209 distinct real ITCH messages (8,929,656 bytes) from 4 files; 11 RTL runs simulated 1,524,451 messages in total (1,009.6 s of Icarus time), comparing 317,104 book snapshots and 15,510 OUCH orders: **all bit-exact**.

**Sources** (NASDAQ historical sample files, via excerpts committed to GitHub; not redistributed here, `ticktotrade fetch-real` downloads them pinned by commit and sha256)

| name | venue, trading day | what the excerpt is | messages | bytes | sha256 |
|---|---|---|---|---|---|
| `psx200k` | Nasdaq PSX TotalView-ITCH 5.0, 2019-12-30 | literal prefix: the first 200,000 messages of 20191230.PSX_ITCH_50 (all symbols, unfiltered), 03:06-09:27 ET; [boyquann/nasdaq-itch50-feed-handler@90d13cb](https://raw.githubusercontent.com/boyquann/nasdaq-itch50-feed-handler/90d13cb55d67954c635e61266a01080206ae93ed/tests/fixture/psx200k.bin) | 200,000 | 5,996,291 | `8e0f88a0a134b25717bf4dbcaff8be8d1539601c1ee98ded6c007e9b03f962a9` |
| `tv_midday` | Nasdaq TotalView-ITCH 5.0, 2019-12-30 | symbol-filtered cut of 12302019.NASDAQ_ITCH50: system events + AAPL/SPY/TSLA directory state from the start of day, order flow for orders added 12:00-12:20 ET; [AarinB1/limitbook@9f01d3e](https://raw.githubusercontent.com/AarinB1/limitbook/9f01d3eacd0ae4d8e159feed828ef93b7189e8de/tests/fixtures/itch50_20191230_midday.itch.gz) | 157,824 | 1,602,440 | `6f164b22abb2e35fee1ab68c3e572cf9311ffb6107adf8fdf7a3fffb79aaa97c` |
| `tv_premarket` | Nasdaq TotalView-ITCH 5.0, 2019-12-30 | symbol-filtered cut of the first ~1.4M messages of 12302019.NASDAQ_ITCH50: system events + the complete stream of 13 symbols, 03:04-05:28 ET; [AarinB1/limitbook@9f01d3e](https://raw.githubusercontent.com/AarinB1/limitbook/9f01d3eacd0ae4d8e159feed828ef93b7189e8de/tests/fixtures/itch50_20191230.itch.gz) | 94,385 | 1,023,282 | `7bd62c758057003046195ec221ac5239374e0dca16cb471c56d270c7d20378b7` |
| `aapl_20200130` | Nasdaq TotalView-ITCH 5.0, 2020-01-30 | the first 10,000 messages of locate 13 (AAPL) in 01302020.NASDAQ_ITCH50, 03:07-07:59 ET; [runk/itch@b29c2f8](https://raw.githubusercontent.com/runk/itch/b29c2f81f9847d63861954bd038ac28719356f12/test/locate-13-10k.bin) | 10,000 | 307,643 | `5d407a266e807e75aa8f6d2cd7427d0eb92183023c0f92fbbf9a0e09eb86a860` |

**Stream** (after MoldUDP64 framing: <= 1 us of ITCH time and <= 1400 B per packet, sent back to back on the 8 B/cycle bus)

| name | time span (ET) | message mix | wire B/msg mean (min-max) | msgs/packet mean (max) | parser msgs/cycle | M msg/s @ 250 MHz |
|---|---|---|---|---|---|---|
| `psx200k` | 03:06:14-09:27:29 | D 39.4%, A 39.3%, R 4.5%, H 4.5%, Y 4.5%, L 3.1% | 30.0 (14-46) | 1.08 (4) | 0.1488 | 37.2 |
| `tv_midday` | 03:04:32-12:19:59 | A 47.4%, D 43.7%, E 3.7%, U 3.6%, P 0.9%, X 0.5% | 30.3 (14-46) | 1.04 (18) | 0.1437 | 35.9 |
| `tv_premarket` | 03:04:32-05:28:02 | A 41.6%, D 40.9%, X 12.1%, U 4.2%, L 0.6%, E 0.4% | 29.4 (14-46) | 1.02 (5) | 0.1461 | 36.5 |
| `aapl_20200130` | 03:07:14-07:59:30 | A 47.6%, D 40.8%, E 8.5%, P 2.4%, L 0.5%, U 0.1% | 30.8 (21-46) | 1.03 (8) | 0.1427 | 35.7 |

**RTL vs golden, per tracked symbol** (whole file through the RTL; default build: 4096-entry table with the XOR index hash, 8 levels/side)

| file / symbol (locate) | book msgs | bit-exact | snapshots / orders | msg->order cycles min/med/max | pkt->order cycles min/med/max | FIFO max | strategy fires per 1k book msgs |
|---|---|---|---|---|---|---|---|
| `psx200k` QQQ (6556) | 8,202 | yes | 8,202 / 2,251 | 4 / 4 / 4 | 9 / 11 / 15 | 0 | 274 |
| `psx200k` SPY (7451) | 6,350 | yes | 6,350 / 454 | 4 / 4 / 5 | 9 / 11 / 15 | 0 | 71 |
| `psx200k` IWM (4315) | 5,882 | yes | 5,882 / 165 | 4 / 4 / 4 | 9 / 11 / 15 | 0 | 28 |
| `tv_midday` SPY (7451) | 72,658 | yes | 72,658 / 4,129 | 4 / 4 / 9 | 9 / 11 / 57 | 1 | 57 |
| `tv_midday` AAPL (13) | 66,364 | yes | 66,364 / 3,636 | 4 / 4 / 7 | 9 / 11 / 38 | 1 | 55 |
| `tv_midday` AAPL (13) (REF_HASH=0) | 66,364 | yes | 66,364 / 2,716 | 4 / 4 / 7 | 9 / 11 / 32 | 1 | 41 |
| `tv_midday` TSLA (7992) | 17,237 | yes | 17,237 / 168 | 4 / 4 / 5 | 9 / 11 / 44 | 0 | 10 |
| `tv_premarket` NVS (5719) | 27,637 | yes | 27,637 / 565 | 4 / 4 / 5 | 9 / 11 / 14 | 0 | 20 |
| `tv_premarket` ASML (547) | 20,426 | yes | 20,426 / 0 | n/a | n/a | 0 | 0 |
| `tv_premarket` BP (992) | 16,279 | yes | 16,279 / 1,352 | 4 / 4 / 6 | 9 / 11 / 15 | 1 | 83 |
| `aapl_20200130` AAPL (13) | 9,705 | yes | 9,705 / 74 | 4 / 4 / 4 | 9 / 11 / 32 | 0 | 8 |

**Finite hardware book vs unbounded reference book** (golden model, whole file; rates per Add/Replace except misses, which are per book message)

| file / symbol | collisions, low-bit index -> XOR hash | misses | level evictions | level drops | BBO exact (px+qty) | BBO price | ref book: max live orders / max levels bid, ask |
|---|---|---|---|---|---|---|---|
| `psx200k` QQQ | 0.0% -> 0.0% | 0.0% | 0.0% | 0.0% | 100.0% | 100.0% | 10 / 5, 5 |
| `psx200k` SPY | 0.2% -> 0.1% | 0.1% | 1.8% | 0.1% | 96.0% | 96.0% | 25 / 12, 14 |
| `psx200k` IWM | 0.0% -> 0.0% | 0.0% | 0.0% | 0.0% | 100.0% | 100.0% | 8 / 3, 5 |
| `tv_midday` SPY | 16.4% -> 4.5% | 2.4% | 3.0% | 4.8% | 56.5% | 97.6% | 261 / 35, 31 |
| `tv_midday` AAPL | 28.8% -> 9.1% | 10.3% | 11.5% | 11.0% | 46.6% | 71.6% | 740 / 181, 176 |
| `tv_midday` TSLA | 21.6% -> 6.1% | 3.6% | 23.7% | 14.0% | 67.6% | 79.9% | 437 / 123, 111 |
| `tv_premarket` NVS | 2.3% -> 0.5% | 0.3% | 0.8% | 0.1% | 93.5% | 98.9% | 59 / 18, 17 |
| `tv_premarket` ASML | 0.8% -> 0.1% | 0.1% | 0.2% | 0.0% | 99.2% | 99.4% | 22 / 15, 10 |
| `tv_premarket` BP | 3.5% -> 0.6% | 0.4% | 0.3% | 0.0% | 91.0% | 98.1% | 66 / 17, 10 |
| `aapl_20200130` AAPL | 11.9% -> 3.5% | 1.8% | 6.2% | 2.7% | 83.1% | 87.6% | 216 / 83, 66 |

**Sizing sweep on the hardest case** (`tv_midday` AAPL, golden model only)

| table entries | levels/side | index | collisions | misses | evictions | drops | BBO exact | BBO price |
|---|---|---|---|---|---|---|---|---|
| 4,096 | 8 | low bits | 10,111 | 13,840 | 3,296 | 2,334 | 20.8% | 51.0% |
| 1,024 | 8 | XOR hash | 10,090 | 13,870 | 3,262 | 2,287 | 21.7% | 51.8% |
| 4,096 | 8 | XOR hash | 3,182 | 6,826 | 4,025 | 3,849 | 46.6% | 71.6% |
| 16,384 | 8 | XOR hash | 802 | 3,850 | 4,372 | 4,706 | 60.3% | 77.0% |
| 65,536 | 8 | XOR hash | 170 | 2,534 | 4,584 | 5,207 | 64.7% | 78.9% |
| 1,048,576 | 8 | XOR hash | 7 | 887 | 5,050 | 5,960 | 65.5% | 79.3% |
| 4,096 | 4 | XOR hash | 3,182 | 6,826 | 5,219 | 7,825 | 34.2% | 58.2% |
| 4,096 | 16 | XOR hash | 3,182 | 6,826 | 2,127 | 1,240 | 56.3% | 82.0% |
| 4,096 | 32 | XOR hash | 3,182 | 6,826 | 1,034 | 426 | 65.1% | 88.1% |
| 65,536 | 32 | XOR hash | 170 | 2,534 | 1,295 | 698 | 92.9% | 98.0% |

Stock directory cross-check between files of the same trading day (the PSX and TotalView files come from different uploaders and venues; 'R' fields after the timestamp compared byte for byte): `psx200k~tv_midday` 3/3 identical 'R' records; `psx200k~tv_premarket` 13/13 identical 'R' records; `tv_midday~tv_premarket` 3/3 identical 'R' records.
<!-- REAL_ITCH:END -->

## Design notes

**Bus and framing.** The input bus carries 8 bytes per cycle. Byte 0 is
`data[7:0]`, the same lane order as AXI-Stream. `keep` is contiguous and only
the last beat is partial. One frame is one MoldUDP64 packet, i.e. the UDP
payload: Session(10) Seq(8) Count(2), then `{Len(2), Msg}` blocks. The
Ethernet/IP/UDP headers (42 B) are assumed to be stripped upstream. The
sequence number is not checked, so gap detection and retransmission are not
implemented.

**Straddling.** Messages and their length prefixes can begin and end at any
byte lane. The prefix itself can be split across two beats (high byte in lane
7, low byte in lane 0 of the next beat). The parser does not step through
bytes serially. It relies on one invariant: *every message is at least 7
bytes long*, and the ITCH 5.0 minimum is 12. So a beat contains at most one
length prefix and at most one message end. Each beat then takes one
comparison (`skip <= n`), one 8:1 byte mux per captured position, and one
small mux for the head of a new message. A length below 7, or a packet that
ends inside a header, prefix or message, increments `cnt_err`. After a length
below 7, the rest of that packet is discarded. Messages of unhandled types
are skipped using the length prefix alone.

**Order table.** The table has 4096 entries of 130 bits: `{valid, ref[64],
side, price, shares}`, which infers 15 RAMB36. It is direct-mapped, and the
full reference is stored as a tag. The index is an XOR hash of the
reference (`REF_HASH=1`): slot bit *i* is the parity of `ref & mask[i]`, with
fixed masks chosen so that any aligned run of references with stride 1, 2,
4, 8 or 16 fills every slot. That costs a 64-input XOR tree per index bit on
the table address path. The first version indexed by `ref[11:0]`, assuming
sequential references make the low bits a near-ideal hash. Real TotalView
data showed that on NASDAQ's main book every order of one symbol has the
same `ref % 4`, so that index could only reach a quarter of the table
([Real ITCH replay](#real-itch-replay)). `REF_HASH=0` keeps the old index for
comparison. The hash has costs. Yosys counts about 500 extra LUTs in
`book_engine`. On the synthetic flow, whose references are dense and
sequential, the low-bit index had no collisions at all, while the hash
behaves like a random index: 8 collisions in the latency run, and the best
bid/offer matched the unbounded book after 95.4% of updates instead of
96.7%. The design is meant for real data, so the hash stays.
*Collision policy:* an Add (or the new half of a Replace) whose slot holds a
different live order is **dropped** and counted. The resident order stays.
Later messages for the dropped order miss and are counted too. A Replace
whose new reference maps to the slot it just vacated is handled explicitly,
because a read and a write hit the same address in the same cycle.
Capacity is limited by the table size and by how long orders stay live, not
by symbol count. The table is sized by `ORDER_BITS`, and tests also run with
16 and 64 entries to force collisions. Reset does not clear the table: it
relies on BRAM initialization. A production design would sweep-clear it.

**Price levels.** Each side keeps `DEPTH` = 8 sorted levels, valid from index
0. All levels are compared in parallel, and one add or subtract is applied
per cycle:
- An add to a new price inserts it and shifts worse levels down. The worst
  level falls off (*eviction*).
- An add worse than a full side is *dropped*.
- A subtract that reaches zero removes the level and shifts worse levels up.
- A subtract at an unknown price does nothing, because that level was
  evicted or dropped earlier.
- Executions are clamped to the order's remaining shares.

Because of these rules, the top of book can differ from an unbounded book
after deep-book churn. The golden model mirrors the rules exactly. The report
also states how often the best bid and offer match an unbounded reference
book.

**Engine.** The FSM has three states: `IDLE` (accept a record, issue the RAM
read), `RD` (data back, write-back and one level operation) and `U2` (the
second half of a Replace). The initiation interval is 2 cycles, or 3 for a
Replace. Every book message is at least 21 bytes on the wire including its
prefix, which is more than 2 beats, so the engine keeps up with the bus. The
8-deep FIFO (with bypass) absorbs local bursts. In the runs, the highest FIFO
occupancy was 1 (see results).

**Strategy (a toy).** Buy at the ask when `bid_qty >= ask_qty << k`, or sell
at the bid when `ask_qty >= bid_qty << k`, and only while
`ask - bid <= max_spread`. It fires only on a false-to-true edge of that
condition, as an IOC order for `min(touch qty, max_qty)`. It is combinational,
so it adds no cycle.

**OUCH out.** The Enter Order message is 49 bytes, sent as 7 beats with the
last beat's keep set to `0x01`. Its fields are:
- token `"TT"` + 12 hex digits of a counter
- side, shares, stock and price
- TIF 0 (IOC), firm, display `Y`, capacity `P`, ISO `N`, min qty 0, cross
  `N`, customer type `N`

Orders that fire while a message is still being sent wait in an 8-deep
queue. Back-to-back messages have no idle cycle between them. SoupBinTCP
framing and the TCP stack are **not** implemented.

**How latency is counted.** A free-running cycle counter stamps the first
beat of each packet (SOF) and the beat that carries each message's last byte
(EOM). The stamps travel with the message through the FIFO, engine, strategy
and order queue. When `ouch_tx` puts the first beat of an order on the
output, it records:
- `lat_pkt` = out cycle - SOF cycle (tick-to-trade as asked: first byte in to first byte out)
- `lat_msg` = out cycle - EOM cycle (pure processing latency)

The harness recomputes both numbers from line numbers in the stimulus file,
without looking at RTL internals, and fails on any mismatch. The processing
floor is **4 cycles**:

| cycle | what happens |
|---|---|
| c0 | beat holding the message's last byte is on the input; the parser registers the message |
| c1 | decode/filter (comb); the engine accepts it via bypass; order-table read issued |
| c2 | table data back; table write-back and price-level update (registered) |
| c3 | strategy evaluates the new top of book (comb) and fires; OUCH beat 0 loaded |
| c4 | first OUCH byte on the output bus |

`lat_pkt` also includes the time to receive the rest of the packet up to the
triggering message, and depends on where that message sits in the packet.
A real system adds MAC/PCS and header parsing on the way in (tens of ns), and
TCP plus MAC/PCS on the way out. Neither is modelled. The parser also takes
its input straight from the bus, with no input register; a production design
at 322 MHz would likely add one or two pipeline registers.

## Verification

- `tests/test_codecs.py` and `tests/test_golden.py`: ITCH lengths match the
  5.0 spec; encode/decode round-trips over every generated message type;
  MoldUDP64 truncation and short-length handling; OUCH field layout; book
  semantics for each message type, collisions, eviction and drop, clamping,
  zero-share adds and same-slot replaces; the strategy's edge triggering; and
  that the generator never crosses the book.
- `tests/test_rtl.py`:
  - a parser unit testbench (`tb/tb_parser.v`) run with arbitrary lengths
    (all lane alignments, split prefixes, the 7-byte case where a message
    starts and ends in one beat) and with malformed packets
  - full-pipeline differential runs over several seeds, with default and
    small tables
  - the directed edge-case scenario (`src/ticktotrade/scenarios.py`)
  - a gapless line-rate run
  - a test that the checker flags a corrupted snapshot, OUCH byte or
    latency value
  - stride-4 order references (the TotalView pattern) with both `REF_HASH`
    builds, and the directed scenario with the old low-bit index
- `tests/test_real_itch.py`: fetch-or-skip tests on the real excerpts:
  structural authenticity checks, the cross-venue stock directory check, the
  `ref % 4` finding, and bit-exact RTL windows of real AAPL (both index
  builds) and SPY flow.
- `ticktotrade mutate` injects 17 plausible RTL bugs (off-by-one lane, wrong
  field offset, missing clamp, the same-slot read-before-write hazard, a
  non-edge-triggered strategy, a latency counter off by one, and others) and
  checks that each is caught. Two earlier mutants that survived turned out to
  be *equivalent*: the mutated code could never change behaviour. They were
  replaced. That analysis is part of the point.

## What is real and what is not

| real | not real / not done |
|---|---|
| Synthesizable Verilog-2005: yosys maps it to Xilinx 7-series cells (LUT/FF/CARRY4/RAMB36) | No place & route, no timing closure. The ns figures use **assumed** clocks. The combinational parser and level arrays would probably need extra pipelining to close at 322 MHz. |
| Cycle-accurate latency in an event-driven simulator (Icarus) | Simulation only; never run on hardware. |
| ITCH 5.0 and MoldUDP64 byte layouts; all 22 ITCH message lengths | No Mold sequence-gap detection or retransmission. |
| **Real** NASDAQ ITCH 5.0 (2019-12-30 PSX prefix, 2019-12-30 and 2020-01-30 TotalView excerpts; 462k messages) replayed bit-exactly through the RTL | Excerpts, not full days: the busiest is symbol-filtered and 20 minutes long, and none covers the open or the close. The Mold framing is reconstructed (real packet boundaries are not in the files), and replay is time-compressed to line rate. Headline latency and throughput tables still come from synthetic flow. |
| Order book semantics for A F E C X D U | One tracked stock locate. Finite table and levels. Collisions drop orders (counted and reported). |
| OUCH 4.2 Enter Order bytes | No SoupBinTCP, no TCP/IP stack, no order state (accept, cancel, fill handling), no risk checks. |
| Tick-to-trade measured from the first byte of the Mold payload | MAC/PCS, Ethernet/IP/UDP parsing and the egress TCP path are excluded; a real system adds them. |
| | The strategy is a **toy**, chosen only to fire often enough to measure latency. It has no alpha. |

## Next steps

- Replay full NASDAQ sample days (this environment could not reach
  emi.nasdaq.com), including the open and close, instead of excerpts.
- A 2- or 4-way set-associative order table and a deeper (or re-fillable)
  level array. Real data shows that both limit book fidelity on busy names.
- Verilator build for 100x faster regressions and longer soak runs.
- Place & route on a real part (for example an Alveo U50/U250 or a
  Kintex-7/UltraScale+ board) to get actual Fmax. Pipeline the parser and
  the level compare as needed.
- An egress path with SoupBinTCP framing, plus an ingress UDP/IP header
  parser and Mold sequence tracking.
- Multiple symbols (a banked table indexed by locate), deeper books, a
  cuckoo or 2-way order table to reduce collisions, and 1-cycle
  initiation-interval engine forwarding.
- Pre-trade risk checks (max notional, fat-finger price band) in the
  order path.

## Layout

```
rtl/        itch_parser.v msg_decode.v sync_fifo.v order_table.v price_levels.v
            book_engine.v strategy.v ouch_tx.v t2t_top.v
tb/         tb_top.v (full pipeline)  tb_parser.v (parser unit)
src/ticktotrade/
            itch.py mold.py ouch.py        codecs
            book.py strategy.py golden.py  bit-exact golden model (+ unbounded ref book)
            gen.py scenarios.py bus.py     stimulus: synthetic market, edge cases, 64b bus
            rtl.py sim.py                  iverilog runner + differential compare
            report.py synth.py mutation.py measurement suite
            replay.py                      real ITCH: pinned sources, Mold framing, replay
            cli.py
tests/      pytest suite
results/    results.json, REPORT.md, synth_xc7_stat.txt (generated)
```

## CI

```bash
sudo apt-get update && sudo apt-get install -y iverilog
cd ticktotrade && pip install -e '.[test]' && pytest -q
```
