"""Pending hardware diagnostic: preload the current position before enable.

This does not modify the production application. Physical zero must be verified
again after the observed reference jump before running with --hardware.
"""
import argparse
import json
from pathlib import Path
import time


def install_hold_initialization(gui_class, events):
    if hasattr(gui_class, '_initialize_enable_hold'):
        raise RuntimeError('Production already initializes enable hold; use run_hardware_validation.py after physical-reference confirmation')
    original = gui_class._motion_state
    def motion_state(self, node, word, mask, expected):
        if word == 0x0F:
            self._check_motion_cancelled()
            # Only preload while switched on but not operation-enabled. Never
            # overwrite an active trajectory's target as a diagnostic side effect.
            status = self._read_statusword(node, fresh=True)
            if status & 0x6F != 0x23:
                raise RuntimeError('Hold initialization requires disabled operation state')
            hold = self._read_actual_position_inc(node, fresh=True)
            self._write_target_position(node, hold)
            actual = int(node.sdo[0x607A].raw)
            self._check_motion_cancelled()
            events.append({'monotonic_s':time.monotonic(), 'event':'pre_enable_hold',
                           'node_id':node.id, 'position_inc':hold, 'readback_inc':actual})
            if actual != hold:
                raise RuntimeError('Hold target readback mismatch; enable cancelled')
        return original(self, node, word, mask, expected)
    gui_class._motion_state = motion_state
    return original


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hardware',action='store_true')
    parser.add_argument('--targets',type=float,nargs='+',required=True)
    parser.add_argument('--speed',type=float,default=5)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not args.hardware:
        parser.error('Real diagnostic is disabled without --hardware and renewed physical-reference confirmation')
    if args.output.exists():
        parser.error('Use a new output path; do not overwrite evidence')
    import run_hardware_validation as validation
    events=[]
    install_hold_initialization(validation.XeryonControlGui,events)
    result=validation.run('targets',args.output,args.speed,50,args.targets)
    result.update(diagnostic_override='pre_enable_current_position_hold',
                  production_code_modified=False, pre_enable_events=events)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True))
    return 0 if result['result']=='PASSED' else 1


if __name__=='__main__':
    raise SystemExit(main())
