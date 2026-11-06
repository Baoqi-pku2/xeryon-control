"""Short comparison of documented set-of-setpoints, without disable or dwell.

The first waypoint is 100 um ahead, the final target 400 um ahead. Existing
profile acceleration=50 and velocity=5 remain. This is diagnostic only.
"""
import argparse
import json
from pathlib import Path
import time
import run_hardware_validation as validation

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
sent = []

def buffered(self, commands):
    for node, final in commands:
        start = self._read_actual_position_inc(node, fresh=True)
        waypoint = start+self._um_to_inc(100)
        if not start < waypoint < final:
            raise RuntimeError('Invalid bounded continuity diagnostic geometry')
        for target in (waypoint, final):
            self._check_motion_cancelled()
            self._write_controlword(node, 0x20F)  # Change on setpoint; buffered, not immediate.
            self._write_target_position(node, target)
            self._write_controlword(node, 0x21F)
            self._wait_setpoint_ack([node], True)
            self._write_controlword(node, 0x20F)
            sent.append({'time': time.monotonic(), 'target_inc': target})
            self._record_motion_event('diagnostic_buffered_target', target_inc=target)
            if target == waypoint:
                if self.test_stop_requested.wait(1):
                    self._check_motion_cancelled()

validation.XeryonControlGui._command_profile_positions = buffered
# Last verified position is 5455 counts => user 5642.2242 um. Both targets
# stay inside the operator-authorized interval, without changing user zero.
result = validation.run('targets', args.output, 5, 50, [6042.2242])
result.update(buffered_targets=sent, acceleration_changed=False, commanded_intermediate_disable=False)
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=True))
raise SystemExit(0 if result['result'] == 'PASSED' else 1)
