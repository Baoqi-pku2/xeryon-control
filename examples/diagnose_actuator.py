"""Read-only CANopen diagnostics for a connected Xeryon actuator."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from canopen import BaseNode402, Network

from settings import CAN_BITRATE, CAN_CHANNEL, CAN_INTERFACE, DEFAULT_SDO_TIMEOUT, INC_PER_MM, NODE_ID


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=CAN_CHANNEL)
    parser.add_argument("--bitrate", type=int, default=int(CAN_BITRATE))
    parser.add_argument("--node", type=int, default=NODE_ID)
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS")
    args = parser.parse_args()

    eds = Path(__file__).resolve().parents[1] / "eds" / "xeryon_xla_5_eds.eds"
    network = Network()
    try:
        network.connect(interface=CAN_INTERFACE, channel=args.channel, bitrate=args.bitrate)
        node = BaseNode402(args.node, str(eds))
        network.add_node(node)
        node.sdo.RESPONSE_TIMEOUT = DEFAULT_SDO_TIMEOUT

        read = lambda index, sub=0: node.sdo[index][sub].raw if sub else node.sdo[index].raw
        um = lambda increments: float(increments) * 1000.0 / float(INC_PER_MM)

        statusword = read(0x6041)
        drive_state = "OPERATION ENABLED" if statusword & 0x6F == 0x27 else "not operation enabled"
        values = [
            ("Node ID", args.node),
            ("Vendor ID", f"0x{read(0x1018, 1):08X}"),
            ("Product code", f"0x{read(0x1018, 2):08X}"),
            ("Revision", f"0x{read(0x1018, 3):08X}"),
            ("Serial number", f"0x{read(0x1018, 4):08X}"),
            ("Error register", f"0x{read(0x1001):02X}"),
            ("Manufacturer status", f"0x{read(0x1002):08X}"),
            ("Heartbeat producer", f"{read(0x1017)} ms"),
            ("Statusword", f"0x{statusword:04X} ({drive_state})"),
            ("Mode display", read(0x6061)),
            ("Position", f"{um(read(0x6064)):.2f} um"),
            ("Position demand", f"{um(read(0x6062)):.2f} um"),
            ("Profile velocity", f"{um(read(0x6081)):.2f} um/s"),
            ("Max profile velocity", f"{um(read(0x607F)):.2f} um/s"),
            ("Profile acceleration", f"{um(read(0x6083)):.2f} um/s^2"),
            ("Max acceleration", f"{um(read(0x60C5)):.2f} um/s^2"),
            ("Quick-stop deceleration", f"{um(read(0x6085)):.2f} um/s^2"),
            ("Following-error window", f"{um(read(0x6065)):.2f} um"),
            ("Position window", f"{um(read(0x6067)):.2f} um"),
            ("Position-window time", f"{read(0x6068)} ms"),
            ("Software limit min", f"{um(read(0x607D, 1)):.2f} um"),
            ("Software limit max", f"{um(read(0x607D, 2)):.2f} um"),
            ("Home offset", f"{um(read(0x607C)):.2f} um"),
            ("Homing method", read(0x6098)),
            ("Homing speed zero", f"{um(read(0x6099, 2)):.2f} um/s"),
            ("Homing acceleration", f"{um(read(0x609A)):.2f} um/s^2"),
            ("Motor frequency min/nom/max", "/".join(str(read(0x2000, sub)) for sub in (1, 2, 3))),
            ("Motor phase min/nom/max", "/".join(str(read(0x2002, sub)) for sub in (1, 2, 3))),
            ("Motor duty min/nom/max", "/".join(str(read(0x2004, sub)) for sub in (1, 2, 3))),
            ("TPDO1 COB/type/inhibit/event", "/".join(str(read(0x1800, sub)) for sub in (1, 2, 3, 5))),
            ("TPDO3 COB/type/inhibit/event", "/".join(str(read(0x1802, sub)) for sub in (1, 2, 3, 5))),
        ]
        for label, value in values:
            print(f"{label:24} {value}")

        if args.watch > 0:
            positions = []
            deadline = time.monotonic() + args.watch
            while time.monotonic() < deadline:
                positions.append(read(0x6064))
                time.sleep(0.25)
            print(f"{'Watch samples':24} {len(positions)}")
            print(f"{'Position drift span':24} {um(max(positions) - min(positions)):.2f} um")
            print(f"{'Final status/error':24} 0x{read(0x6041):04X} / 0x{read(0x1001):02X}")
    finally:
        network.disconnect()


if __name__ == "__main__":
    main()
