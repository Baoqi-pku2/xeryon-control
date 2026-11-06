"""Bounded protocol comparisons at the unchanged 50 um/s^2 acceleration.

Single uninterrupted target per move; no segmented motion or automatic retries.
"""
import argparse
import json
import time
from pathlib import Path
import run_hardware_validation as validation

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--variant', choices=('immediate', 'sdo', 'jerk', 'poll'), required=True)
parser.add_argument('--poll-ms', type=int, default=100)
parser.add_argument('--poll-rest-ms', type=int, default=100)
parser.add_argument('--jerk', type=float, default=500)
parser.add_argument('--speed', type=float, default=300)
parser.add_argument('--targets', nargs='+', type=float)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()

def command(self, commands):
    base = 0x2F if args.variant == 'immediate' else 0x0F
    def word(node, value):
        if args.variant == 'sdo':
            with self.controlword_lock:
                self._check_motion_cancelled()
                node.sdo[0x6040].raw = value
        else:
            self._write_controlword(node, value)
    for node, _ in commands:
        word(node, base)
    for node, target in commands:
        if args.variant == 'sdo':
            with self.controlword_lock:
                self._check_motion_cancelled()
                node.sdo[0x607A].raw = target
        else:
            self._write_target_position(node, target)
    for node, _ in commands:
        word(node, base | 0x10)
    self._wait_setpoint_ack([node for node, _ in commands], True)
    for node, _ in commands:
        word(node, base)

jerk_state = {}
if args.variant == 'jerk':
    original_profile = validation.XeryonControlGui._write_profile
    original_disconnect = validation.XeryonControlGui._disconnect_worker
    def write_profile(self, node, profile):
        if 'original_raw' not in jerk_state:
            jerk_state['original_raw'] = int(node.sdo[0x60A4][1].raw)
        # Production now programs its own jerk. Apply the diagnostic override
        # afterwards, while still disabled, so its readback is the actual test.
        original_profile(self, node, profile)
        requested = self._um_to_inc(args.jerk)
        node.sdo[0x60A4][1].raw = requested
        actual = int(node.sdo[0x60A4][1].raw)
        if actual != requested:
            raise RuntimeError('Jerk readback mismatch')
        jerk_state.update(test_raw=actual, test_um_s3=args.jerk)
        profile.update(jerk=args.jerk, jerk_inc=actual)
    def disconnect(self):
        if self.node is not None and 'original_raw' in jerk_state:
            self._disable_drive_worker()
            self.node.sdo[0x60A4][1].raw = jerk_state['original_raw']
            jerk_state['restored'] = int(self.node.sdo[0x60A4][1].raw) == jerk_state['original_raw']
            if not jerk_state['restored']:
                raise RuntimeError('Original jerk restoration not confirmed')
        original_disconnect(self)
    validation.XeryonControlGui._write_profile = write_profile
    validation.XeryonControlGui._disconnect_worker = disconnect
elif args.variant in ('immediate', 'sdo'):
    validation.XeryonControlGui._command_profile_positions = command
else:
    if not 50 <= args.poll_ms <= 500:
        parser.error('--poll-ms must be between 50 and 500')
    if not 50 <= args.poll_rest_ms <= 500:
        parser.error('--poll-rest-ms must be between 50 and 500')
    validation.control_gui.POLL_INTERVAL_MS = args.poll_ms
    original_poll = validation.XeryonControlGui._poll_status
    original_status_worker = validation.XeryonControlGui._status_worker
    def poll(self):
        if time.monotonic() < self.__dict__.get('_diagnostic_poll_ready', 0):
            self.after(args.poll_ms, self._poll_status)
            return
        original_poll(self)
    def status_worker(self):
        try:
            return original_status_worker(self)
        finally:
            self._diagnostic_poll_ready = time.monotonic() + args.poll_rest_ms / 1000
    validation.XeryonControlGui._poll_status = poll
    validation.XeryonControlGui._status_worker = status_worker
result = validation.run('targets' if args.targets else 'local', args.output, args.speed, 50, args.targets,
                        tick_ms=73 if args.variant=='poll' else 100)
result.update(diagnostic_variant=args.variant, acceleration_changed=False, segmented_motion=False)
if args.variant == 'poll':
    result['diagnostic_poll_ms'] = args.poll_ms
    result['diagnostic_poll_rest_ms'] = args.poll_rest_ms
if jerk_state:
    result['jerk_diagnostic'] = jerk_state
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=True))
raise SystemExit(0 if result['result'] == 'PASSED' else 1)
