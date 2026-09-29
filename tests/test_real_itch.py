"""Real NASDAQ ITCH 5.0 bytes (fetch-or-skip).

No real market data is committed: NASDAQ's terms for its historical sample
files do not clearly allow redistribution.  The files are excerpts of those
samples that other projects published on GitHub; `replay.fetch` downloads
them pinned by commit and sha256 into the gitignored data/real/.  Without
network access (or if an upstream repo disappears) these tests skip.
"""
import pytest

from conftest import requires_iverilog
from ticktotrade import replay


def _source(name):
    src = replay.SOURCES[name]
    try:
        return replay.ensure_source(src, allow_fetch=True, log=lambda *a: None)
    except Exception as e:  # no network, upstream gone, ...
        pytest.skip(f"real ITCH source {name} unavailable: {e}")


@pytest.mark.parametrize("name", list(replay.SOURCES))
def test_real_source_looks_like_nasdaq_itch(name):
    p = _source(name)
    msgs = replay.load_messages(p)
    a = replay.authenticity(msgs)
    assert a["all_lengths_match_spec"] and a["timestamps_monotonic"]
    d = replay.stock_directory(msgs)
    for sym in replay.SOURCES[name].symbols:
        assert sym in d.values()
    if name != "aapl_20200130":          # a single-locate cut has no system events
        assert a["first_system_event"] == "O"


def test_same_day_directories_agree_across_venues():
    """PSX and TotalView files of 2019-12-30, published by different people,
    carry identical 'R' records for every locate they share."""
    cc = replay.cross_check_directories({"psx200k": _source("psx200k"),
                                         "tv_premarket": _source("tv_premarket")})
    v = cc["psx200k~tv_premarket"]
    assert v["common_locates"] == 13 and v["identical_R_fields"] == 13


def test_totalview_refs_share_residue_mod4_per_symbol():
    """The finding behind the XOR order-table index: on NASDAQ TotalView each
    symbol's order references are all congruent mod 4 (PSX: no such pattern)."""
    tv = replay.authenticity(replay.load_messages(_source("tv_midday")))
    assert all(len(v) == 1 for v in tv["add_ref_mod4_per_locate"].values())
    psx = replay.authenticity(replay.load_messages(_source("psx200k")))
    assert all(v == [0, 1, 2, 3] for v in psx["add_ref_mod4_per_locate"].values())


@requires_iverilog
@pytest.mark.parametrize("ref_hash", [1, 0])
def test_real_aapl_rtl_bit_exact(ref_hash):
    r = replay.replay_file(_source("aapl_20200130"), ["AAPL"], rtl_messages=4000,
                           ref_hash=ref_hash, sweep=False, build_tag="_test",
                           log=lambda *a: None)
    x = r["symbols"]["AAPL"]["rtl"]
    assert x["pass"], x["errors"]
    assert x["snapshots_compared"] > 3000 and x["orders_compared"] > 0


@requires_iverilog
def test_real_midday_spy_rtl_bit_exact():
    r = replay.replay_file(_source("tv_midday"), ["SPY"], rtl_messages=6000, sweep=False,
                           build_tag="_test", log=lambda *a: None)
    x = r["symbols"]["SPY"]["rtl"]
    assert x["pass"], x["errors"]
    assert x["golden_counters"]["collisions"] >= 0 and x["snapshots_compared"] > 1000
