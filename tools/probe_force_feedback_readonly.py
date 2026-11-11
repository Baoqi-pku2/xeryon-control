"""Bounded force-feedback capability audit. SDO uploads only; no motion."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
from xeryon_control.config import EDS_PATH
import time

import canopen
from canopen.sdo.exceptions import SdoAbortedError, SdoCommunicationError

ROOT = Path(__file__).resolve().parents[1]
OBJECTS = [
    (0x6041, 0, "statusword", False),
    (0x1018, 1, "vendor", False), (0x1018, 2, "product", False),
    (0x1018, 3, "revision", False), (0x1018, 4, "serial", False),
    (0x5004, 0, "firmware", None),
    (0x6064, 0, "position_actual", True),
    (0x6062, 0, "position_demand", True),
    (0x60F4, 0, "following_error", True),
    (0x606C, 0, "velocity_actual_candidate", True),
    (0x6077, 0, "torque_actual_candidate", True),
    (0x6078, 0, "current_actual_candidate", True),
    (0x6074, 0, "torque_demand_candidate", True),
    (0x6071, 0, "target_torque_candidate", True),
    (0x6075, 0, "motor_rated_current_candidate", False),
    (0x6076, 0, "motor_rated_torque_candidate", False),
    (0x6079, 0, "dc_link_voltage_candidate", False),
    (0x6502, 0, "supported_modes", False),
    (0x6060, 0, "mode_requested", True),
    (0x6061, 0, "mode_display", True),
    (0x1001, 0, "error_register", False),
    (0x1002, 0, "manufacturer_status", False),
    (0x6065, 0, "following_error_window", False),
    (0x6066, 0, "following_error_timeout", False),
    (0x5000, 0, "logging_flags", False),
    (0x5001, 0, "logging_interval", False),
    (0x5003, 0, "trace_subindex_count", False),
] + [(idx, sub, f"{name}_{label}", True)
     for idx, name in [(0x2000, "frequency"), (0x2002, "phase"), (0x2004, "duty")]
     for sub, label in [(1, "min"), (2, "nominal"), (3, "max")]]
ALLOWED = {(i, s) for i, s, _, _ in OBJECTS}


def allowed_frame(can_id, data, remote=False):
    if can_id != 0x621 or remote or len(data) != 8:
        return False
    if data[0] == 0x40:
        return ((int.from_bytes(data[1:3], "little"), data[3]) in ALLOWED
                and not any(data[4:]))
    return data[0] in (0x60, 0x70) and not any(data[1:])


def check_guard():
    assert allowed_frame(0x621, bytes.fromhex("40 41 60 00 00 00 00 00"))
    assert allowed_frame(0x621, bytes.fromhex("60 00 00 00 00 00 00 00"))
    for cob, payload in [(0, "01 21"), (0x221, "0f 00 00 00 00 00 00 00"),
                         (0x621, "2b 40 60 00 0f 00 00 00"),
                         (0x621, "23 7a 60 00 00 00 00 00"),
                         (0x621, "40 00 20 04 00 00 00 00"),
                         (0x621, "80 00 00 00 00 00 00 00")]:
        assert not allowed_frame(cob, bytes.fromhex(payload))


def run(output):
    check_guard()
    output.mkdir(parents=True, exist_ok=False)
    report = {"started_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "channel": "COM3", "node": 33, "bitrate": 125000,
              "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "guard_self_check": "passed", "objects": {}, "samples": [],
              "sent_frames": [], "received_sdo_frames": [], "blocked_frames": []}
    network = canopen.Network()
    send = network.send_message
    def guarded_send(can_id, data, remote=False):
        record = {"t": time.monotonic(), "can_id": hex(can_id), "data": bytes(data).hex()}
        if not allowed_frame(can_id, data, remote):
            report["blocked_frames"].append(record)
            raise RuntimeError("Read-only guard blocked non-whitelisted upload")
        send(can_id, data, remote)
        report["sent_frames"].append(record)
    network.send_message = guarded_send
    def receive(can_id, data, stamp):
        report["received_sdo_frames"].append({"timestamp": stamp, "data": bytes(data).hex()})
    network.subscribe(0x5A1, receive)
    try:
        network.connect(interface="slcan", channel="COM3", bitrate=125000)
        node = network.add_node(33, EDS_PATH)
        node.sdo.RESPONSE_TIMEOUT = 0.4
        node.sdo.MAX_RETRIES = 1
        # Suppress the library's automatic outbound abort on a timeout.
        node.sdo.abort = lambda *a, **k: None
        def read(item):
            idx, sub, name, signed = item
            start = time.monotonic()
            result = {"index": f"0x{idx:04X}", "subindex": sub, "name": name}
            try:
                raw = node.sdo.upload(idx, sub)
                result.update(status="read", raw_hex=raw.hex(),
                              value=raw.decode("utf-8", errors="replace").rstrip("\0")
                              if signed is None else int.from_bytes(raw, "little", signed=signed))
            except SdoAbortedError as exc:
                result.update(status="sdo_abort", abort_code=f"0x{exc.code:08X}", error=str(exc))
            except SdoCommunicationError:
                raise  # Missing response is not evidence that an object is absent.
            result["duration_s"] = time.monotonic() - start
            return result
        for item in OBJECTS:
            result = read(item)
            report["objects"][item[2]] = result
            if item[2] == "statusword":
                if result["status"] != "read":
                    raise RuntimeError("Cannot establish initial drive state")
                if result["value"] & 4:
                    raise RuntimeError("Drive enabled; diagnostic stopped without state writes")
            time.sleep(0.04)
        supported = [o for o in OBJECTS if o[2] in
                     ("statusword", "position_actual", "position_demand", "following_error")
                     and report["objects"][o[2]]["status"] == "read"]
        for _ in range(25):
            sample = {"t_monotonic_s": time.monotonic(), "values": {}}
            for item in supported:
                sample["values"][item[2]] = read(item)
            report["samples"].append(sample)
            if sample["values"]["statusword"].get("value", 4) & 4:
                raise RuntimeError("Enabled state observed; diagnostic stopped")
            time.sleep(0.2)
        report["completed"] = True
    except Exception as exc:
        report.update(completed=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            network.disconnect()
        except Exception as exc:
            report["disconnect_error"] = str(exc)
        report["finished_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        report["all_sent_frames_are_sdo_uploads"] = all(
            allowed_frame(int(f["can_id"], 16), bytes.fromhex(f["data"]))
            for f in report["sent_frames"])
        (output / "interface_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in
                     ("samples", "sent_frames", "received_sdo_frames")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-guard", action="store_true")
    args = parser.parse_args()
    if args.check_guard:
        check_guard()
        print("Read-only frame guard checks passed; no hardware accessed.")
    elif args.output:
        run(args.output)
    else:
        parser.error("--output or --check-guard is required")
