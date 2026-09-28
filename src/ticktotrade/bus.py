"""Serialize MoldUDP64 packets onto the 64-bit / 8-byte-per-beat stream.

Byte k of a beat is data[8k+7:8k] (AXI-Stream lane order); keep is
contiguous from lane 0 and only the final beat of a packet is partial.
One line of the stimulus file per clock cycle: "valid last keep data" (hex).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class Stimulus:
    lines: list = field(default_factory=list)          # (valid, last, keep, data)
    pkt_first_line: list = field(default_factory=list)  # per packet
    pkt_beat_lines: list = field(default_factory=list)  # per packet: line index of each beat

    def byte_line(self, pkt_idx: int, offset: int) -> int:
        """Cycle (line index) on which byte `offset` of packet `pkt_idx` is presented."""
        return self.pkt_beat_lines[pkt_idx][offset // 8]

    def write(self, path) -> None:
        with open(path, "w") as f:
            f.write("".join(f"{v:x} {l:x} {k:02x} {d:016x}\n" for v, l, k, d in self.lines))


def build_stimulus(packets: list[bytes], rng: random.Random | None = None,
                   gap_prob: float = 0.0, ipg_max: int = 0, lead_idle: int = 2) -> Stimulus:
    """gap_prob: probability of an idle (valid=0) cycle before any beat;
    ipg_max: extra idle cycles (uniform 0..ipg_max) between packets."""
    rng = rng or random.Random(0)
    st = Stimulus()
    idle = (0, 0, 0, 0)
    st.lines.extend([idle] * lead_idle)
    for pkt in packets:
        if ipg_max:
            st.lines.extend([idle] * rng.randint(0, ipg_max))
        beats = []
        nb = (len(pkt) + 7) // 8
        for b in range(nb):
            while gap_prob and rng.random() < gap_prob:
                st.lines.append(idle)
            chunk = pkt[8 * b: 8 * b + 8]
            keep = (1 << len(chunk)) - 1
            beats.append(len(st.lines))
            st.lines.append((1, int(b == nb - 1), keep, int.from_bytes(chunk, "little")))
        st.pkt_first_line.append(beats[0])
        st.pkt_beat_lines.append(beats)
    return st
