"""Bounded SDO uploads only: never NMT, enable, target or parameter writes."""
import argparse
import json
from datetime import datetime
from pathlib import Path

import canopen


def read_snapshot(channel, output, extended=False):
    network = canopen.Network()
    original_send = network.send_message
    def read_only_send(can_id, data, remote=False):
        upload_request = len(data) == 8 and (data[0] == 0x40 or
                         (data[0] in (0x60, 0x70) and not any(data[1:])))
        if can_id != 0x600 + 33 or remote or not upload_request:
            raise RuntimeError("Read-only guard rejected a non-SDO-upload frame")
        original_send(can_id, data, remote)
    network.send_message = read_only_send
    report = {"time": datetime.now().astimezone().isoformat(), "channel": channel,
              "node_id": 33, "motion_or_parameter_writes": False, "objects": {}}
    try:
        network.connect(interface="slcan", channel=channel, bitrate=125000)
        node = canopen.RemoteNode(33, str(Path(__file__).resolve().parents[1]/"eds/xeryon_xla_5_eds.eds"))
        network.add_node(node)
        node.sdo.RESPONSE_TIMEOUT = 0.3
        node.sdo.MAX_RETRIES = 1
        for index in (0x6041, 0x6040, 0x6064, 0x6062, 0x607A, 0x6060, 0x6061,
                      0x6081, 0x6083, 0x6084, 0x6085, 0x6067, 0x6068, 0x1001, 0x1002):
            try:
                raw = node.sdo.upload(index, 0)
                report["objects"][f"0x{index:04X}"] = int.from_bytes(
                    raw, "little", signed=index in (0x6064,0x6062,0x607A,0x6060,0x6061))
            except Exception as exc:
                report["objects"][f"0x{index:04X}"] = {"error": str(exc)}
        for index, subindex in ((0x5004, 0), (0x60A4, 1), (0x60C5, 0), (0x607F, 0)):
            key = f"0x{index:04X}:{subindex}"
            try:
                raw = node.sdo.upload(index, subindex)
                report["objects"][key] = (raw.decode('utf-8', errors='replace').rstrip('\x00')
                                         if index == 0x5004 else int.from_bytes(raw, 'little'))
            except Exception as exc:
                report["objects"][key] = {"error": str(exc)}
        if extended:
            report['extended_objects'] = {}
            addresses = [(i, 0) for i in (0x3000, 0x5002, 0x6502, 0x607C,
                         0x6063, 0x60FC, 0x60F4, 0x2011, 0x2012, 0x605A, 0x605B, 0x605C, 0x605D, 0x605E)]
            addresses += [(i, s) for i, count in ((0x1018, 4), (0x3001, 5), (0x4000, 10),
                          (0x2000, 3), (0x2002, 3), (0x2004, 3), (0x607D, 2)) for s in range(1, count+1)]
            for index, subindex in addresses:
                key = f'0x{index:04X}:{subindex}'
                try:
                    raw = node.sdo.upload(index, subindex)
                    report['extended_objects'][key] = {'raw_hex': raw.hex(),
                        'value': int.from_bytes(raw, 'little', signed=index in (0x607C,0x6063,0x60FC,0x60F4,0x607D))}
                except Exception as exc:
                    report['extended_objects'][key] = {'error': str(exc)}
    except Exception as exc:
        report["connection_error"] = str(exc)
    finally:
        network.disconnect()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="COM3")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extended", action="store_true", help="Read documented I/O, profile and motor configuration; never write")
    args = parser.parse_args()
    print(json.dumps(read_snapshot(args.channel, args.output, args.extended), ensure_ascii=False, indent=2))
