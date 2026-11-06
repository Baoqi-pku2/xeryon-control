"""Bounded unloaded comparison with the manufacturer's acknowledgement handshake.

Only the setpoint handshake differs; normal GUI guards, arrival and STOP remain.
"""
import argparse
import json
import time
from pathlib import Path
import run_hardware_validation as validation

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--speed', type=float, default=20)
args = parser.parse_args()
observations = []

def acknowledged_command(self, commands):
    for node, target in commands:
        self._write_controlword(node, 0x0F)
        self._write_target_position(node, target)
    for node, _ in commands:
        self._write_controlword(node, 0x1F)
    deadline = time.monotonic() + 2
    pending = {node.id for node, _ in commands}
    while pending and time.monotonic() < deadline:
        self._check_motion_cancelled()
        for node, _ in commands:
            if node.id not in pending:
                continue
            status = self._read_statusword(node, fresh=True)
            observations.append({'time': time.monotonic(), 'node': node.id, 'statusword': status})
            if status & ((1 << 3) | (1 << 13)):
                raise RuntimeError('Drive fault during setpoint acknowledgement')
            if status & (1 << 12):
                pending.remove(node.id)
        self.test_stop_requested.wait(0.01)
    for node, _ in commands:
        self._write_controlword(node, 0x0F)
    if pending:
        raise RuntimeError('Setpoint acknowledgement not received within 2 seconds')

validation.XeryonControlGui._command_profile_positions = acknowledged_command
result = validation.run('local', args.output, args.speed, 50)
args.output.with_suffix('.handshake.json').write_text(json.dumps(observations, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=True, indent=2))
raise SystemExit(0 if result['result'] == 'PASSED' else 1)
