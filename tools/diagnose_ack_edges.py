"""Observe fresh status at each production handshake phase; all guards remain."""
import argparse
import json
import time
from pathlib import Path
import run_hardware_validation as validation

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
observations = []
original_wait = validation.XeryonControlGui._wait_setpoint_ack

def observed_wait(self, nodes, expected):
    original_read = self._read_statusword
    def read(node, **kwargs):
        value = original_read(node, **kwargs)
        observations.append({'time': time.monotonic(), 'expected_ack': expected,
                             'node': node.id, 'statusword': value})
        return value
    self._read_statusword = read
    try:
        return original_wait(self, nodes, expected)
    finally:
        self._read_statusword = original_read

validation.XeryonControlGui._wait_setpoint_ack = observed_wait
result = validation.run('local', args.output, 5, 50)
args.output.with_suffix('.ack_edges.json').write_text(json.dumps(observations, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=True))
raise SystemExit(0 if result['result'] == 'PASSED' else 1)
