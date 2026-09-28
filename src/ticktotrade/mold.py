"""MoldUDP64 downstream packet framing.

Header: Session(10 alpha) SequenceNumber(8) MessageCount(2), then
MessageCount blocks of ``MessageLength(2) MessageData``.  Count 0 is a
heartbeat, 0xFFFF signals end of session.
"""
from __future__ import annotations

import struct

HEADER_LEN = 20
MIN_MSG_LEN = 7   # parser invariant: <= 1 message end per 8-byte beat
_HDR = struct.Struct(">10sQH")


def encode_packet(session: bytes | str, seq: int, msgs: list[bytes], count: int | None = None) -> bytes:
    if isinstance(session, str):
        session = session.encode("ascii")
    session = session.ljust(10, b" ")[:10]
    out = [_HDR.pack(session, seq, len(msgs) if count is None else count)]
    for m in msgs:
        out.append(struct.pack(">H", len(m)))
        out.append(m)
    return b"".join(out)


def decode_packet(pkt: bytes):
    """Return (session, seq, count, msgs) for a well formed packet."""
    session, seq, count = _HDR.unpack_from(pkt, 0)
    msgs = []
    pos = HEADER_LEN
    while pos < len(pkt):
        (ln,) = struct.unpack_from(">H", pkt, pos)
        pos += 2
        msgs.append(pkt[pos:pos + ln])
        pos += ln
    return session, seq, count, msgs


def split_messages(pkt: bytes):
    """Hardware-equivalent message extraction.

    Returns (list of (offset_of_first_msg_byte, msg_bytes), error_flag).
    Mirrors rtl/itch_parser.v: header skipped; a message or length prefix
    truncated by the end of the packet is dropped and flagged (as is a packet
    shorter than the header); a MessageLength < MIN_MSG_LEN (7) is flagged and
    the rest of the packet is discarded.  At most one flag per packet.
    """
    out = []
    if len(pkt) < HEADER_LEN:
        return out, True
    pos = HEADER_LEN
    while pos < len(pkt):
        if pos + 2 > len(pkt):
            return out, True
        ln = (pkt[pos] << 8) | pkt[pos + 1]
        pos += 2
        if ln < MIN_MSG_LEN:
            return out, True
        if pos + ln > len(pkt):
            return out, True
        out.append((pos, pkt[pos:pos + ln]))
        pos += ln
    return out, False
