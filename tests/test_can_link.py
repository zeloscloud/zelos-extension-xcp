"""XCP on CAN: ids from the config or the A2L, and the DAQ frame time estimate."""

import pytest

from zelos_extension_xcp import a2l
from zelos_extension_xcp.can_link import (
    EXTENDED,
    bitrates,
    fd,
    foreign_ids,
    frame_seconds,
    ids,
    max_dlc_required,
)

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


def test_a2l_ids_carry_their_flags_apart():
    section = {
        "protocol": "CAN",
        "can_id_master": 0x18DA00F1,
        "can_id_master_extended": True,
        "can_id_slave": None,
        "can_id_slave_extended": False,
        "bitrate": None,
        "can_fd": {"max_dlc": 64, "data_bitrate": None, "max_dlc_required": True},
        "daq_list_can_ids": [(1, 0x7F5, False, False)],
        "event_can_ids": [(0, 0x7F1, False, False)],
    }
    catalog = {"transports": [section]}
    with pytest.raises(ValueError, match="Response CAN ID.*CAN_ID_SLAVE"):
        ids({}, catalog)
    assert ids({"rx_id": "18DAF100", "extended_ids": True}, catalog) == (
        0x18DA00F1 | EXTENDED,
        0x18DAF100 | EXTENDED,
    )
    assert bitrates({}, catalog) is None and max_dlc_required({}, catalog)
    # List 0 and event 0 arrive on the response id; list 1 does not.
    assert foreign_ids({}, catalog, 1, [0], 0x7F1) is None
    assert "DAQ list 1 on CAN id 0x7F5" in foreign_ids({}, catalog, 2, [0], 0x7F1)


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


def test_two_can_transports_need_explicit_ids(tmp_path):
    pytest.importorskip("zelos_can.a2l")
    render = pytest.importorskip("zelos_extension_xcp.demo.a2l").render
    text = render()
    begin, end = text.index("/begin XCP_ON_CAN"), text.index("/end XCP_ON_CAN")
    second = text[begin:end].replace("CAN_ID_MASTER 0x7F0", "CAN_ID_MASTER 0x600")
    second = second.replace("CAN_ID_SLAVE 0x7F1", "CAN_ID_SLAVE 0x601")
    second = second.replace("BAUDRATE 500000", "BAUDRATE 250000")
    path = tmp_path / "two.a2l"
    path.write_text(text[:begin] + second + "/end XCP_ON_CAN\n" + text[begin:])
    catalog = a2l.load(str(path))
    assert sum(t["protocol"] == "CAN" for t in catalog["transports"]) == 2
    with pytest.raises(ValueError, match="0x600/0x601, 0x7F0/0x7F1"):
        ids({}, catalog)
    with pytest.raises(ValueError, match="match none"):
        ids({"tx_id": "0x602", "rx_id": "0x603"}, catalog)
    picked = {"tx_id": "0x600", "rx_id": "0x601"}
    assert ids(picked, catalog) == (0x600, 0x601)
    assert bitrates(picked, catalog) == (250_000, 250_000)
    assert bitrates({"tx_id": "0x7F0", "rx_id": "0x7F1"}, catalog) == (500_000, 500_000)
