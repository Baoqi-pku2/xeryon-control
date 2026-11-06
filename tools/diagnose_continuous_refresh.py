"""Probe same-final-target refresh while enabled; never restart a failed move.

No intermediate target, disable, dwell or acceleration change is introduced.
Existing stall/overshoot checks run before the healthy-motion callback, and
neither the progress watchdog nor the original movement deadline is reset.
"""
import argparse
import json
from pathlib import Path
import time
import run_hardware_validation as validation

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--target', type=float, required=True)
parser.add_argument('--interval', type=float, default=10)
parser.add_argument('--speed', type=float, default=5)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
observations = []
original_snapshot = validation.XeryonControlGui._record_controller_snapshot
last_refresh = None

def healthy_snapshot(self, node):
    global last_refresh
    original_snapshot(self, node)
    if self.active_target_inc is None:
        return
    now = time.monotonic()
    if last_refresh is None:
        last_refresh = self.active_target_time
    if now-last_refresh < args.interval:
        return
    self._check_motion_cancelled()
    if now-self.active_last_progress_time > 2:
        raise RuntimeError('Refusing target refresh without recent progress')
    target = self.active_target_inc
    before = int(node.sdo[0x6064].raw)
    if int(node.sdo[0x607A].raw) != target:
        raise RuntimeError('Device target differs before refresh')
    deadline = self.active_target_deadline
    self._write_controlword(node, 0x2F)
    self._write_target_position(node, target)
    self._write_controlword(node, 0x3F)
    self._wait_setpoint_ack([node], True)
    self._write_controlword(node, 0x2F)
    last_refresh = time.monotonic()
    item = {'elapsed_motion_s': last_refresh-self.active_target_time, 'target_inc': target,
            'before_inc': before, 'after_inc': int(node.sdo[0x6064].raw),
            'statusword': self._read_statusword(node, fresh=True),
            'original_deadline_unchanged': deadline == self.active_target_deadline}
    observations.append(item)
    self._record_motion_event('diagnostic_same_target_refresh', **item)

validation.XeryonControlGui._record_controller_snapshot = healthy_snapshot
result = validation.run('targets', args.output, args.speed, 50, [args.target])
result.update(continuous_same_target_refresh=observations, refresh_interval_s=args.interval,
              intermediate_targets=False, acceleration_changed=False)
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=True))
raise SystemExit(0 if result['result'] == 'PASSED' else 1)
