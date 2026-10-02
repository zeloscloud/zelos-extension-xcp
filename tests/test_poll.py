"""Polled reads: touching ranges merged into one command, never across a gap."""

from zelos_extension_xcp.a2l import signal
from zelos_extension_xcp.poll import frames, limit, runs


def sig(name, address, datatype="UBYTE", ext=0):
    m = {"name": name, "address": address, "address_extension": ext, "datatype": datatype,
         "byte_order": "little", "dims": [], "conversion": {"kind": "IDENTICAL"}}  # fmt: skip
    return signal(m)


def test_touching_ranges_merge_gaps_and_extensions_do_not():
    word, flag, far, other = (
        sig("word", 0x100, "UWORD"),
        sig("flag", 0x101),  # inside the word
        sig("far", 0x103),  # one byte gap
        sig("other", 0x102, ext=1),
    )
    got, skipped = runs([far, word, flag, other, sig("next", 0x102)], limit(8, False))
    assert [(r.ext, r.address, r.size) for r in got] == [(0, 0x100, 4), (1, 0x102, 1)]
    assert [(s.name, o) for s, o in got[0].signals] == [
        ("word", 0), ("flag", 1), ("next", 2), ("far", 3)
    ]  # fmt: skip
    assert skipped == {}


def test_reads_sized_to_the_packet_or_block_mode():
    longs = [sig(f"v{i}", 0x200 + 4 * i, "ULONG") for i in range(8)]  # 32 contiguous bytes
    small, _ = runs(longs, limit(8, False))
    assert [r.size for r in small] == [4] * 8 and frames(small, 8) == 16
    block, _ = runs(longs, limit(8, True))
    assert [r.size for r in block] == [32] and frames(block, 8) == 3 + 5
    _, skipped = runs([sig("u64", 0x300, "A_UINT64")], limit(8, False))
    assert "no block mode" in skipped["u64"]
