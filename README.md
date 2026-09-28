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
pytest -q                               # ~15 s; RTL tests skip if iverilog is missing
ticktotrade sim --messages 20000 --seed 7
ticktotrade report                      # measurement suite -> results/ (+ this README's table)
ticktotrade report --skip-synth --skip-mutation   # fast version (~30 s)
ticktotrade mutate                      # mutation smoke test only
ticktotrade sim --itch-file 01302019.NASDAQ_ITCH50.gz --messages 200000 --locate 13
```

`ticktotrade sim` prints PASS/FAIL, the book counters, the latency
distribution and throughput. Stimulus and RTL dumps go to `build/`, which git
ignores. `--itch-file` replays a NASDAQ binary ITCH 5.0 sample file
(length-prefixed, optionally gzipped). None is bundled or downloaded, and
real data has not been run through this repo.

## Results

Everything between the markers below is generated by `ticktotrade report`
from `results/results.json`. `tests/test_report.py` fails if this README
drifts from that file. More detail is in [results/REPORT.md](results/REPORT.md).

<!-- RESULTS:BEGIN -->
Generated by `ticktotrade report` from `results/results.json` (2026-09-28T22:45:09Z). Clock frequencies are **assumptions** used to convert cycles to ns; no place & route or timing analysis was run.

**Tick-to-trade latency** (realistic run: 30,000 ITCH messages, 12,109 MoldUDP64 packets, random idle cycles on the bus; 564 orders fired)

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

- measurement runs: 4/4 pass; small-table regression (64-entry table, depth 4): 8/8 seeds pass (2410 collisions, 337 level evictions, 582 level drops, 4172 unknown-order misses exercised)
- directed edge cases: pass; adversarial parser framing: 4/4 pass
- 67,256 book snapshots and 1,926 OUCH orders compared in total
- hardware book (4096-entry direct-mapped table, 8 levels/side) matched an unbounded reference book's best bid/offer after 96.74% of updates in the latency run
- mutation smoke test: 16/16 injected RTL bugs detected

**Resources** (Yosys 0.33, `synth_xilinx -family xc7`, pre-place-and-route estimate, default parameters)

| LUT | FF | CARRY4 | MUXF7/F8 | LUTRAM (RAM32M) | BRAM36 |
|---|---|---|---|---|---|
| 6,919 | 3,203 | 660 | 569/203 | 76 | 15 |

Per module (self, LUT/FF): price_levels 3239/1040, itch_parser 1559/941, book_engine 1101/589, ouch_tx 442/573, strategy 436/2, msg_decode 107/0, sync_fifo 34/26, t2t_top 1/32
<!-- RESULTS:END -->

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
side, price, shares}`, which infers 15 RAMB36. It is direct-mapped on
`ref[11:0]`. NASDAQ assigns order references sequentially across all
symbols, so the low bits make a near-ideal hash. The full reference is
stored as a tag.
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
- `ticktotrade mutate` injects 16 plausible RTL bugs (off-by-one lane, wrong
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
| ITCH 5.0 and MoldUDP64 byte layouts; all 22 ITCH message lengths | **Synthetic** market data. Real ITCH replay is supported (`--itch-file`) but was not run here. No Mold sequence-gap detection or retransmission. |
| Order book semantics for A F E C X D U | One tracked stock locate. Finite table and levels. Collisions drop orders (counted and reported). |
| OUCH 4.2 Enter Order bytes | No SoupBinTCP, no TCP/IP stack, no order state (accept, cancel, fill handling), no risk checks. |
| Tick-to-trade measured from the first byte of the Mold payload | MAC/PCS, Ethernet/IP/UDP parsing and the egress TCP path are excluded; a real system adds them. |
| | The strategy is a **toy**, chosen only to fire often enough to measure latency. It has no alpha. |

## Next steps

- Replay real NASDAQ ITCH sample days, with per-symbol locate discovery from
  `R` messages and a report of how often collisions and evictions happen
  with real order lifetimes.
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
            cli.py
tests/      pytest suite
results/    results.json, REPORT.md, synth_xc7_stat.txt (generated)
```

## CI

```bash
sudo apt-get update && sudo apt-get install -y iverilog
cd ticktotrade && pip install -e '.[test]' && pytest -q
```
