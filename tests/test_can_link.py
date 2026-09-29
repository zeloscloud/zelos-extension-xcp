"""XCP on CAN: ids from the config or the A2L, and the DAQ frame time estimate."""

import pytest

from zelos_extension_xcp.can_link import EXTENDED, fd, frame_seconds, ids

A2L = {
    "transports": [
        {
            "protocol": "CAN",
            "can_id_master": 0x7F0,
            "can_id_slave": 0x7F1,
            "bitrate": 500_000,
            "can_fd": None,
        }
    ]
}


def test_ids_config_over_a2l_never_guessed():
    assert ids({}, A2L) == (0x7F0, 0x7F1)
    assert ids({"tx_id": "0x600", "rx_id": "601"}, A2L) == (0x600, 0x601)
    assert ids({"tx_id": "18DA00F1", "rx_id": "18DAF100", "extended_ids": True}, None) == (
        0x18DA00F1 | EXTENDED,
        0x18DAF100 | EXTENDED,
    )
    with pytest.raises(ValueError, match="Response CAN ID"):
        ids({"tx_id": "0x600"}, None)
    with pytest.raises(ValueError, match="Extended IDs"):
        ids({"tx_id": "0x800", "rx_id": "0x801"}, None)


def test_fd_must_agree_with_the_a2l():
    assert fd({}, A2L) is False
    with pytest.raises(ValueError, match="classic CAN"):
        fd({"fd_mode": True}, A2L)


def test_frame_time_worst_case_stuffing():
    # standard id, 8 bytes: 34 + 64 + 13 + 24 = 135 bits
    assert frame_seconds(3, False, False, (500_000, 500_000)) == pytest.approx(135 / 500_000)
    # extended id: 54 + 64 + 13 + 29 = 160 bits
    assert frame_seconds(8, True, False, (1_000_000, 1_000_000)) == pytest.approx(160e-6)
    assert frame_seconds(64, False, True, (500_000, 2_000_000)) < frame_seconds(
        64, False, True, (500_000, 500_000)
    )
