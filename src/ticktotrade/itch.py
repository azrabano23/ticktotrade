"""NASDAQ TotalView-ITCH 5.0 message encoder / decoder.

Every message starts with Type(1) StockLocate(2) TrackingNumber(2)
Timestamp(6, ns since midnight).  Integers are big endian, alpha fields are
left-justified, space padded ASCII.  Prices are 4-decimal fixed point
(``Price(4)``: $1.00 == 10000).
"""
from __future__ import annotations

import gzip
import struct
from pathlib import Path
from typing import Iterator

# field codes: u1/u2/u4/u8 unsigned ints, ts = 6-byte timestamp, aN = N-byte alpha
_COMMON = [("stock_locate", "u2"), ("tracking", "u2"), ("timestamp", "ts")]

SPECS: dict[str, list[tuple[str, str]]] = {
    "S": [("event_code", "a1")],
    "R": [("stock", "a8"), ("market_category", "a1"), ("fin_status", "a1"),
          ("round_lot_size", "u4"), ("round_lots_only", "a1"), ("issue_class", "a1"),
          ("issue_subtype", "a2"), ("authenticity", "a1"), ("short_sale_threshold", "a1"),
          ("ipo_flag", "a1"), ("luld_tier", "a1"), ("etp_flag", "a1"),
          ("etp_leverage", "u4"), ("inverse", "a1")],
    "H": [("stock", "a8"), ("trading_state", "a1"), ("reserved", "a1"), ("reason", "a4")],
    "Y": [("stock", "a8"), ("reg_sho_action", "a1")],
    "L": [("mpid", "a4"), ("stock", "a8"), ("primary_mm", "a1"), ("mm_mode", "a1"),
          ("mp_state", "a1")],
    "V": [("level1", "u8"), ("level2", "u8"), ("level3", "u8")],
    "W": [("breached_level", "a1")],
    "K": [("stock", "a8"), ("ipo_release_time", "u4"), ("ipo_release_qualifier", "a1"),
          ("ipo_price", "u4")],
    "J": [("stock", "a8"), ("ref_price", "u4"), ("upper_price", "u4"),
          ("lower_price", "u4"), ("extension", "u4")],
    "h": [("stock", "a8"), ("market_code", "a1"), ("halt_action", "a1")],
    "A": [("order_ref", "u8"), ("side", "a1"), ("shares", "u4"), ("stock", "a8"),
          ("price", "u4")],
    "F": [("order_ref", "u8"), ("side", "a1"), ("shares", "u4"), ("stock", "a8"),
          ("price", "u4"), ("attribution", "a4")],
    "E": [("order_ref", "u8"), ("executed_shares", "u4"), ("match", "u8")],
    "C": [("order_ref", "u8"), ("executed_shares", "u4"), ("match", "u8"),
          ("printable", "a1"), ("exec_price", "u4")],
    "X": [("order_ref", "u8"), ("cancelled_shares", "u4")],
    "D": [("order_ref", "u8")],
    "U": [("order_ref", "u8"), ("new_order_ref", "u8"), ("shares", "u4"), ("price", "u4")],
    "P": [("order_ref", "u8"), ("side", "a1"), ("shares", "u4"), ("stock", "a8"),
          ("price", "u4"), ("match", "u8")],
    "Q": [("shares", "u8"), ("stock", "a8"), ("cross_price", "u4"), ("match", "u8"),
          ("cross_type", "a1")],
    "B": [("match", "u8")],
    "I": [("paired_shares", "u8"), ("imbalance_shares", "u8"), ("imbalance_dir", "a1"),
          ("stock", "a8"), ("far_price", "u4"), ("near_price", "u4"),
          ("current_ref_price", "u4"), ("cross_type", "a1"), ("price_variation", "a1")],
    "N": [("stock", "a8"), ("interest_flag", "a1")],
}

BOOK_TYPES = frozenset("AFECXDU")

_FMT = {"u1": "B", "u2": "H", "u4": "I", "u8": "Q", "ts": "HI"}


def _compile(fields):
    fmt = ">c"
    for _, code in _COMMON + fields:
        fmt += _FMT.get(code) or f"{int(code[1:])}s"
    return struct.Struct(fmt)


_STRUCTS = {t: _compile(f) for t, f in SPECS.items()}
LENGTHS = {t: s.size for t, s in _STRUCTS.items()}


def _alpha(v, n):
    if isinstance(v, str):
        v = v.encode("ascii")
    if len(v) > n:
        raise ValueError(f"alpha field too long: {v!r} > {n}")
    return v.ljust(n, b" ")


def encode(msg: dict) -> bytes:
    """Encode a message dict (must contain 'type' and all fields of the spec)."""
    t = msg["type"]
    fields = _COMMON + SPECS[t]
    vals: list = [t.encode()]
    for name, code in fields:
        v = msg.get(name, 0 if code[0] in "ut" else b"")
        if code == "ts":
            vals += [(v >> 32) & 0xFFFF, v & 0xFFFFFFFF]
        elif code[0] == "a":
            vals.append(_alpha(v, int(code[1:])))
        else:
            vals.append(int(v))
    return _STRUCTS[t].pack(*vals)


def decode(data: bytes) -> dict:
    """Decode one ITCH message.  Unknown types return {'type', 'raw'}."""
    t = chr(data[0])
    st = _STRUCTS.get(t)
    if st is None or len(data) != st.size:
        return {"type": t, "raw": bytes(data)}
    vals = list(st.unpack(data))
    out: dict = {"type": t}
    i = 1
    for name, code in _COMMON + SPECS[t]:
        if code == "ts":
            out[name] = (vals[i] << 32) | vals[i + 1]
            i += 2
        else:
            v = vals[i]
            out[name] = v.decode("ascii") if isinstance(v, bytes) else v
            i += 1
    return out


def read_itch_file(path: str | Path, limit: int | None = None) -> Iterator[bytes]:
    """Yield raw messages from a NASDAQ binary ITCH 5.0 file (optionally .gz).

    The historical sample files published by NASDAQ are a stream of
    ``Length(2, BE) Message(Length)`` records - the same framing used inside
    MoldUDP64 message blocks.
    """
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    n = 0
    with opener(p, "rb") as f:
        while limit is None or n < limit:
            hdr = f.read(2)
            if len(hdr) < 2:
                return
            (ln,) = struct.unpack(">H", hdr)
            body = f.read(ln)
            if len(body) < ln:
                return
            yield body
            n += 1


def write_itch_file(path: str | Path, msgs) -> None:
    with open(path, "wb") as f:
        for m in msgs:
            f.write(struct.pack(">H", len(m)) + m)
