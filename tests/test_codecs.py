"""ITCH / MoldUDP64 / OUCH encoders and decoders."""
import random

import pytest

from ticktotrade import gen, itch, mold, ouch

SPEC_LENGTHS = {"S": 12, "R": 39, "H": 25, "Y": 20, "L": 26, "V": 35, "W": 12, "K": 28,
                "J": 35, "h": 21, "A": 36, "F": 40, "E": 31, "C": 36, "X": 23, "D": 19,
                "U": 35, "P": 44, "Q": 40, "B": 19, "I": 50, "N": 20}


def test_itch_lengths_match_spec():
    assert itch.LENGTHS == SPEC_LENGTHS


def test_itch_roundtrip_all_generated_types():
    msgs, _ = gen.generate(gen.GenConfig(n_messages=3000, seed=5))
    seen = set()
    for m in msgs:
        d = itch.decode(m)
        assert "raw" not in d
        assert itch.encode(d) == m
        seen.add(d["type"])
    assert set("AFECXDU") <= seen
    assert len(seen - set("AFECXDU")) >= 8   # filler of many other types


def test_itch_add_order_field_layout():
    m = itch.encode({"type": "A", "stock_locate": 0x0102, "tracking": 3,
                     "timestamp": 0x0000_1234_5678_9ABC, "order_ref": 0x1122334455667788,
                     "side": "S", "shares": 500, "stock": "AAPL", "price": 1_234_500})
    assert len(m) == 36
    assert m[0:1] == b"A" and m[1:3] == b"\x01\x02" and m[3:5] == b"\x00\x03"
    assert m[5:11] == bytes.fromhex("123456789abc")
    assert m[11:19] == bytes.fromhex("1122334455667788")
    assert m[19:20] == b"S" and int.from_bytes(m[20:24], "big") == 500
    assert m[24:32] == b"AAPL    " and int.from_bytes(m[32:36], "big") == 1_234_500


def test_itch_file_roundtrip(tmp_path):
    msgs, _ = gen.generate(gen.GenConfig(n_messages=200, seed=2))
    p = tmp_path / "x.itch"
    itch.write_itch_file(p, msgs)
    assert list(itch.read_itch_file(p)) == msgs
    assert list(itch.read_itch_file(p, limit=10)) == msgs[:10]


def test_mold_roundtrip_and_split():
    msgs = [itch.encode({"type": "D", "stock_locate": 1, "order_ref": i}) for i in range(5)]
    pkt = mold.encode_packet("SESSION1", 42, msgs)
    session, seq, count, out = mold.decode_packet(pkt)
    assert session == b"SESSION1  " and seq == 42 and count == 5 and out == msgs
    split, err = mold.split_messages(pkt)
    assert not err and [m for _, m in split] == msgs
    assert split[0][0] == 22                    # 20-byte header + 2-byte length


@pytest.mark.parametrize("cut", [5, 19, 21, 22, 30, 40])
def test_mold_split_truncation_flags_error(cut):
    msgs = [itch.encode({"type": "D", "stock_locate": 1, "order_ref": 1})] * 2
    pkt = mold.encode_packet("S", 1, msgs)[:cut]
    split, err = mold.split_messages(pkt)
    assert err
    assert all(off + len(m) <= cut for off, m in split)


def test_mold_heartbeat_and_short_length():
    hb = mold.encode_packet("S", 7, [])
    assert mold.split_messages(hb) == ([], False)
    bad = hb + b"\x00\x03abc" + b"\x00\x0cSxxxxxxxxxxx"
    assert mold.split_messages(bad) == ([], True)     # MsgLen < 7 -> discard rest


def test_packetize_preserves_messages():
    msgs, pkts = gen.generate(gen.GenConfig(n_messages=500, seed=9))
    got = [m for p in pkts for _, m in mold.split_messages(p)[0]]
    assert got == msgs


def test_ouch_enter_order():
    b = ouch.encode_enter_order(ouch.token_for(0xABC), "B", 300, "MSFT", 4_100_500, firm="TTRD")
    assert len(b) == 49
    d = ouch.decode_enter_order(b)
    assert d["type"] == "O" and d["token"] == "TT000000000ABC" and d["side"] == "B"
    assert d["shares"] == 300 and d["stock"] == "MSFT    " and d["price"] == 4_100_500
    assert d["tif"] == 0 and d["firm"] == "TTRD" and d["display"] == "Y"
    assert d["capacity"] == "P" and d["iso"] == "N" and d["min_qty"] == 0
    assert d["cross_type"] == "N" and d["customer_type"] == "N"
