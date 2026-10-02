"""`demo-ecu`: run the demo XCP ECU standalone."""

import logging
import signal
import threading

import rich_click as click

from zelos_extension_xcp.demo import DemoEcu, model

logger = logging.getLogger(__name__)


def _can_id(ctx: click.Context, param: click.Parameter, value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as e:
        raise click.BadParameter(f"{value!r} is not an id, e.g. 0x7F0") from e


@click.command("demo-ecu")
@click.option(
    "--transport",
    type=click.Choice(["can", "udp", "tcp"]),
    default="can",
    show_default=True,
    help="XCP on CAN, or on Ethernet (udp, tcp)",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Ethernet: bind address")
@click.option("--port", "-p", type=int, default=5555, show_default=True, help="Ethernet: port")
@click.option(
    "--interface",
    default=None,
    help="CAN: python-can interface (socketcan, pcan, ...). Default: the zelos-can virtual bus",
)
@click.option("--channel", default="xcp-demo", show_default=True, help="CAN: channel")
@click.option("--bitrate", type=int, default=None, help="CAN: bitrate, for --interface")
@click.option("--fd", is_flag=True, help="CAN: CAN FD frames, up to 64 bytes")
@click.option(
    "--rx-id",
    default=hex(model.CAN_ID_MASTER),
    show_default=True,
    callback=_can_id,
    help="CAN: id the ECU receives (master to slave)",
)
@click.option(
    "--tx-id",
    default=hex(model.CAN_ID_SLAVE),
    show_default=True,
    callback=_can_id,
    help="CAN: id the ECU sends (slave to master)",
)
@click.option("--extended", is_flag=True, help="CAN: 29-bit ids")
@click.option(
    "--timestamp-size",
    type=click.Choice(["0", "1", "2", "4"]),
    default="4",
    show_default=True,
    help="DAQ timestamp bytes, 0 for none",
)
def demo_ecu(
    transport: str,
    host: str,
    port: int,
    interface: str | None,
    channel: str,
    bitrate: int | None,
    fd: bool,
    rx_id: int,
    tx_id: int,
    extended: bool,
    timestamp_size: str,
) -> None:
    """Run the demo ECU: a measurement-only XCP slave with its A2L.

    Point any XCP master at it, with the A2L it prints. Stop with Ctrl-C.

    \b
    Example:
        uv run main.py demo-ecu --interface socketcan --channel vcan0
        uv run main.py demo-ecu --transport udp
        uv run main.py demo-ecu --transport tcp -p 5556
    """
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        ecu = DemoEcu(
            host,
            port,
            transport,
            channel=channel,
            interface=interface,
            bitrate=bitrate,
            can_id_master=rx_id,
            can_id_slave=tx_id,
            can_extended=extended,
            can_fd=fd,
            timestamp_size=int(timestamp_size),
        ).start()
    except (OSError, ImportError, ValueError) as e:
        raise click.ClickException(f"cannot start the demo ECU on {transport}: {e}") from e
    except Exception as e:  # python-can raises its own errors for a missing adapter
        raise click.ClickException(f"cannot open CAN {interface} {channel}: {e}") from e
    logger.info("A2L: %s", ecu.a2l_path)
    if transport == "can" and interface is None:
        logger.warning("The zelos-can virtual bus is in-process: other programs cannot reach it")
    try:
        while not stop.wait(0.5):
            pass
    finally:
        ecu.stop()
