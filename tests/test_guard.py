"""Command allowlist at the framing choke point."""

import pytest

from zelos_extension_xcp.compat import Command
from zelos_extension_xcp.guard import ALLOWED, CommandRefused, GuardedFraming, refusal


@pytest.mark.parametrize(
    "cmd",
    [
        Command.DOWNLOAD,
        Command.SHORT_DOWNLOAD,
        Command.MODIFY_BITS,
        Command.SET_CAL_PAGE,
        Command.COPY_CAL_PAGE,
        Command.SET_REQUEST,
        Command.GET_SEED,
        Command.UNLOCK,
        Command.PROGRAM_START,
        Command.USER_CMD,
        Command.TRANSPORT_LAYER_CMD,
        Command.WRITE_DAQ_MULTIPLE,
        Command.SET_SEGMENT_MODE,
    ],
)
def test_refused(cmd):
    assert refusal(cmd, ()) is not None


def test_allowed_and_stim_direction():
    assert all(refusal(c, (0,)) is None for c in ALLOWED)
    assert refusal(Command.SET_DAQ_LIST_MODE, (0x10, 0, 0, 0, 0, 1, 0)) is None
    assert "STIM" in refusal(Command.SET_DAQ_LIST_MODE, (0x12, 0, 0, 0, 0, 1, 0))


def test_refused_before_framing():
    framed = []

    class Inner:
        counter_send = 7

        def prepare_request(self, cmd, *data):
            framed.append(cmd)
            return b"frame"

    guarded = GuardedFraming(Inner())
    with pytest.raises(CommandRefused):
        guarded.prepare_request(Command.DOWNLOAD, 1, 2)
    assert guarded.prepare_request(Command.SHORT_UPLOAD, 4, 0, 0, 0, 0, 0, 0) == b"frame"
    assert framed == [Command.SHORT_UPLOAD]
    assert guarded.counter_send == 7
