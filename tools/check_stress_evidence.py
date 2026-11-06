"""Independently reconcile stress results with per-motion controller traces."""
import argparse
import collections
import json
from pathlib import Path


def validate(folder, require_hardware=False, minimum_count=500):
    result = json.loads((folder/'result.json').read_text(encoding='utf-8'))
    plan = json.loads((folder/'plan.json').read_text(encoding='utf-8'))
    failures, traces = [], []
    def check(condition, message):
        if not condition:
            failures.append(message)
    check(result['result'] == 'PASSED', 'Driver result is not PASSED')
    check(not result['errors'], 'Driver recorded errors')
    check(result['shutdown_completed'], 'Final shutdown unconfirmed')
    check(not require_hardware or result['hardware_motion'], 'Simulation cannot satisfy hardware acceptance')
    expected = [step for batch in plan['batches'] for step in batch['steps']]
    check(plan['count'] == len(expected) == result['planned_count'], 'Plan/result declared counts disagree')
    check(len(expected) >= minimum_count, f'Fewer than {minimum_count} planned targets')
    if plan.get('acceptance',{}).get('single_final_source_required'):
        check(bool(result.get('source_manifest')), 'Frozen source manifest is missing')
        check(result.get('source_unchanged') is True, 'Source changed or was not checked')
        check(result.get('completed_count') == plan['acceptance']['completed_count'], 'Acceptance count not completed')
    reference_count = result.get('reference_count', -143)
    check(len(result['completed']) == len(expected), 'Incomplete result list')
    for path in (folder/'XeryonControl/results').glob('motion_*.jsonl'):
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        commands = [row for row in rows if row['event'] == 'command']
        check(len(commands) == 1, f'{path.name}: expected one uninterrupted command')
        if commands:
            traces.append((commands[0], rows, path.name))
    traces.sort(key=lambda item: item[0]['monotonic_s'])
    check(len(traces) == len(expected), 'Trace command count differs from plan')
    velocity_counts, errors_um, dwells, nonzero = collections.Counter(), [], [], 0
    refresh_count = 0
    previous_disabled = None
    for step, (command, rows, name) in zip(expected, traces):
        resolution = command['um_per_count']
        target = round((reference_count*resolution+step['target_user_um'])/resolution)
        profile = command['profile']
        check(command['target_inc'] == target, f'{name}: target differs from frozen plan')
        check(profile['velocity'] == step['speed_um_s'], f'{name}: velocity differs from plan')
        check(profile['acceleration'] == 50, f'{name}: acceleration changed')
        check(profile.get('jerk') == 500, f'{name}: finite jerk missing or changed')
        check(not any(r['event'] in ('stop','communication_fault','communication_stop','disable_failed',
                                    'position_reference_changed') for r in rows),
              f'{name}: protective termination occurred')
        refreshes = [r for r in rows if r['event'] == 'same_target_refresh']
        check(not refreshes, f'{name}: repeated target refresh is not an uninterrupted single command')
        check(all(r['target_inc'] == target for r in refreshes), f'{name}: refresh changed final target')
        check(len({r['original_deadline'] for r in refreshes}) <= 1, f'{name}: refresh extended deadline')
        refresh_count += len(refreshes)
        reached = [r for r in rows if r['event'] == 'target_reached']
        check(len(reached) == 1, f'{name}: missing/duplicate verified arrival')
        if reached:
            arrival = reached[0]
            check(bool(arrival['statusword'] & (1 << 10)), f'{name}: target-reached bit absent')
            check(abs(arrival['position_inc']-target) <= profile['position_window_inc'], f'{name}: arrival outside window')
            disabled = [r for r in rows if r['event'] == 'stop_verified' and r['monotonic_s'] >= arrival['monotonic_s']]
            check(bool(disabled), f'{name}: no verified disable after arrival')
            if disabled:
                stop = disabled[0]
                check(abs(stop['position_inc']-target) <= profile['position_window_inc'], f'{name}: stopped outside window')
                check((stop['statusword'] & 0x6F) != 0x27, f'{name}: drive still enabled')
                if previous_disabled is not None:
                    check(command['monotonic_s'] >= previous_disabled, f'{name}: overlapping motions')
                previous_disabled = stop['monotonic_s']
                errors_um.append((stop['position_inc']-target)*resolution)
        starts = [r for r in rows if r['event'] == 'sequence_dwell_started']
        ends = [r for r in rows if r['event'] == 'sequence_dwell_finished']
        for start, end in zip(starts, ends):
            elapsed = end['monotonic_s']-start['monotonic_s']
            check(elapsed >= start['seconds'], f'{name}: dwell too short')
            dwells.append(elapsed)
        check(len(starts) == len(ends), f'{name}: incomplete sequence dwell')
        velocity_counts[f"{float(profile['velocity']):g}"] += 1
        nonzero += int(command['start_inc'] != target)
    expected_dwells = sum(len(b['steps'])-1 for b in plan['batches'] if b['kind'] == 'sequence')
    check(len(dwells) == expected_dwells, 'Sequence dwell count differs from plan')
    return {'passed': not failures, 'hardware_motion': result['hardware_motion'], 'failures': failures,
            'planned_count': len(expected), 'trace_command_count': len(traces), 'nonzero_distance_commands': nonzero,
            'same_final_target_refreshes': refresh_count,
            'velocity_counts': dict(velocity_counts), 'sequence_dwells': len(dwells),
            'dwell_min_s': min(dwells, default=None), 'dwell_max_s': max(dwells, default=None),
            'max_abs_stopped_error_um': max(map(abs, errors_um), default=None),
            'start_clicks': result['start_clicks'], 'elapsed_s': result['elapsed_s'],
            'ui_tick_max_gap_s': result['ui_tick_max_gap_s']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--require-hardware', action='store_true')
    parser.add_argument('--minimum-count',type=int,default=500)
    args = parser.parse_args()
    report = validate(args.folder, args.require_hardware, args.minimum_count)
    (args.folder/'evidence_check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))
    raise SystemExit(0 if report['passed'] else 1)
