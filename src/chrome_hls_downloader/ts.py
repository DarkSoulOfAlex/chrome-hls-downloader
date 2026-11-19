"""MPEG-TS helpers: completeness check and deliberate corruption for the self-test."""

import random

PACKET = 188
SYNC = 0x47


def is_complete_ts(data):
    """True if ``data`` is a whole number of TS packets, each starting with the sync byte."""
    n = len(data)
    if n < PACKET or n % PACKET:
        return False
    return data[0::PACKET] == bytes([SYNC]) * (n // PACKET)


def is_complete_ts_file(path):
    with open(path, "rb") as f:
        return is_complete_ts(f.read())


def packet_pid(data, packet_index):
    o = packet_index * PACKET
    return ((data[o + 1] & 0x1F) << 8) | data[o + 2]


def corrupt_sample(data, packets=60, every=5, start_fraction=0.5, seed=7):
    """Return ``(corrupted_copy, n_corrupted)``.

    Overwrites the payload of some packets of the busiest PID (the video) with random
    bytes, keeping the 4-byte TS headers, PES starts and adaptation fields intact so the
    file still demuxes but the decoder receives garbage slice data.
    """
    data = bytearray(data)
    n = len(data) // PACKET
    if n == 0:
        return data, 0
    counts = {}
    for p in range(n):
        pid = packet_pid(data, p)
        counts[pid] = counts.get(pid, 0) + 1
    vpid = max(counts, key=counts.get)
    rnd = random.Random(seed)
    done = eligible = 0
    for p in range(int(n * start_fraction), n):
        if done >= packets:
            break
        o = p * PACKET
        if packet_pid(data, p) != vpid or data[o + 1] & 0x40 or data[o + 3] & 0x20:
            continue
        eligible += 1
        if eligible % every:
            continue
        for i in range(o + 24, o + PACKET):
            data[i] = rnd.randrange(256)
        done += 1
    return data, done
