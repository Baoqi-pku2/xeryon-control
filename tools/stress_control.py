"""Seeded, resumable-by-new-run GUI stress validation; never retries a failure.

Real CAN is opened only with --hardware. Simulation is explicitly labelled and
uses accelerated ideal motion and zero dwell; it is not hardware validation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples'))
import control_gui
import shortcut_presets
from verify_sequence_flow import SimulatedNode


def write_progress_report(path, report):
    """Keep the previous valid report if a Windows reader briefly locks it."""
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        temp.replace(path)
    except PermissionError:
        return False
    return True


def generate_plan(seed, count):
    rng = random.Random(seed)
    steps, batches = [], []
    current = 50.395
    # Uniform global relocations alternate with local velocity-balanced probes.
    # Long low-speed probes are explicit; do not hide them in short-step counts.
    while len(steps) < count:
        batch_id = len(batches)
        size = min(1 if batch_id % 6 == 0 else 7, count-len(steps))
        dwell = rng.choice((0, 0, 0, 0.2, 0.5, 1, 3))
        if batch_id % 20 == 19:
            dwell = 30
        batch = {'id': batch_id+1, 'kind': 'single' if size == 1 else 'sequence',
                 'dwell_s': dwell, 'steps': []}
        for local in range(size):
            index = len(steps)
            if index in (100, 300):
                speed, category = 5, 'long_low_speed'
                target = rng.uniform(7000, 12000) if current < 6000 else rng.uniform(50, 2000)
            elif local == 0:
                speed, category = rng.choice((100, 300)), 'global_relocation'
                target = rng.uniform(50, 12000)
            else:
                speed, category = (3, 5, 10, 20, 50, 100, 300)[index % 7], 'local_velocity_probe'
                distance = max(12, speed*rng.uniform(1, 5))
                direction = rng.choice((-1, 1))
                target = current + direction*distance
                if not 50 <= target <= 12000:
                    target = current - direction*distance
                target = max(50, min(12000, target))
            if index == 0 or index == count-1:
                target, category = 50, 'endpoint'
            elif index == count//2:
                target, speed, category = 12000, 300, 'endpoint'
            step = {'index': index+1, 'target_user_um': round(target, 4), 'speed_um_s': speed,
                    'category': category, 'planned_distance_um': abs(target-current)}
            current = target
            steps.append(step)
            batch['steps'].append(step)
        batches.append(batch)
    return {'seed': seed, 'count': count, 'sampling': 'stratified; global uniform relocations + local probes + long low-speed probes',
            'target_range_um': [50, 12000], 'speed_choices_um_s': [3, 5, 10, 20, 50, 100, 300],
            'estimated_motion_and_dwell_s': sum(s['planned_distance_um']/s['speed_um_s'] for s in steps)
                + sum(b['dwell_s']*max(1, len(b['steps'])-1) for b in batches), 'batches': batches}


def select_plan_range(plan, first, last=None):
    """Select remaining cases only after a separately reviewed stopped run.

    Original targets, speeds and case numbers remain immutable. This does not
    turn failed cases into passes or resume motion automatically.
    """
    last = plan['count'] if last is None else last
    if not 1 <= first <= last <= plan['count']:
        raise ValueError('Invalid original test-case range')
    if first == 1 and last == plan['count']:
        return plan
    selected = dict(plan, original_plan_count=plan['count'], original_case_range=[first, last])
    selected['batches'] = []
    index = 0
    for batch in plan['batches']:
        steps = []
        for step in batch['steps']:
            if first <= step['index'] <= last:
                index += 1
                steps.append(dict(step, index=index, original_index=step['index']))
        if steps:
            selected['batches'].append(dict(batch, steps=steps, kind='single' if len(steps)==1 else 'sequence'))
    selected['count'] = index
    selected['estimated_motion_and_dwell_s'] = (
        sum(s['planned_distance_um']/s['speed_um_s'] for b in selected['batches'] for s in b['steps'])
        + sum(b['dwell_s']*max(1,len(b['steps'])-1) for b in selected['batches']))
    return selected


def run(plan, output, hardware, acceleration, reference_count=-143):
    output = output.resolve()
    if (output/'result.json').exists() or (output/'events.jsonl').exists():
        raise ValueError('Use a new output directory; previous evidence must not be overwritten')
    output.mkdir(parents=True, exist_ok=True)
    os.environ['LOCALAPPDATA'] = str(output)
    (output/'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    if not hardware:
        control_gui.POLL_INTERVAL_MS = 30
        control_gui.STATUS_POLL_REST_SECONDS = 0
    app = control_gui.XeryonControlGui()
    app.title(app.title() + (' · 实机压力测试 / STOP 可中止' if hardware else ' · 模拟压力测试'))
    if not hardware:
        app.withdraw()
        origin = (app._um_to_inc(app._inc_to_um(reference_count)+plan['initial_fixture_user_um'])
                  if 'initial_fixture_user_um' in plan else -93)
        node = SimulatedNode(origin=origin)
        app.node, app.nodes = node, {'Actuator A': node}
    app.channel_var.set('COM3')
    app.node_id_var.set('33')
    app.reset_on_connect_var.set(False)
    app.node_b_enabled_var.set(False)
    app.encoder_var.set('1MU')
    app.encoder_verified_var.set(True)
    app.acceleration_var.set(str(acceleration))
    highest_speed = max(s['speed_um_s'] for b in plan['batches'] for s in b['steps'])
    app.max_velocity_var.set(str(max(float(app.max_velocity_var.get()), highest_speed)))
    app.max_acceleration_var.set(str(max(500, acceleration)))
    report = {'hardware_motion': hardware, 'simulation_dwell_s': None if hardware else 0,
              'seed': plan['seed'], 'planned_count': plan['count'], 'acceleration_um_s2': acceleration,
              'reference_count': reference_count,
              'source_sha256': hashlib.sha256((ROOT/'examples/control_gui.py').read_bytes()).hexdigest(),
              'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'completed': [], 'errors': [], 'start_clicks': 0, 'submitted': [], 'result': 'RUNNING'}
    source_paths = sorted((ROOT/'examples').glob('*.py')) + sorted((ROOT/'examples/common').glob('*.py'))
    report['source_manifest'] = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    started, last_tick, last_save = time.monotonic(), time.monotonic(), 0
    verified_stops = {}
    original_record = app._record_motion_event
    def record(event, **values):
        original_record(event, **values)
        if event == 'stop_verified':
            verified_stops[values['node_id']] = dict(values)
    app._record_motion_event = record
    batch_index, batch, state = 0, None, 'connecting'
    next_at, finished, max_tick_gap = 0, False, 0
    last_successful_save = started

    def emit(event, **values):
        with (output/'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'elapsed_s': time.monotonic()-started, 'event': event, **values}, ensure_ascii=False)+'\n')

    def save():
        nonlocal last_successful_save
        report.update(elapsed_s=time.monotonic()-started, completed_count=len(report['completed']),
                      ui_tick_max_gap_s=max_tick_gap, final_position_inc=app.last_position_inc,
                      target_status=app.target_status_var.get(), sequence_status=app.shortcut_status_var.get())
        if write_progress_report(output/'progress.json', report):
            last_successful_save = time.monotonic()
        else:
            report['progress_write_deferrals'] = report.get('progress_write_deferrals', 0)+1
            if time.monotonic()-last_successful_save > 30:
                raise RuntimeError('Progress report locked for more than 30 seconds')

    def finish(result):
        nonlocal finished
        if finished:
            return
        finished = True
        report['result'] = result
        save()
        (output/'ui.log').write_text(app.log_text.get('1.0', 'end'), encoding='utf-8')
        app._on_close()

    original_error, original_done, original_start = app._show_error, app._handle_shortcut_done, app._start_shortcut_step

    def error(message):
        report['errors'].append(str(message))
        emit('error', message=str(message))
        original_error(message)
    app._show_error = error

    def callback_error(kind, value, tb):
        error(f'Tk callback exception: {kind.__name__}: {value}')
        app.stop_actuator_test()
    app.report_callback_exception = callback_error

    def complete(step):
        if step['index'] != len(report['completed'])+1:
            raise RuntimeError('Duplicate, stale or out-of-order completion')
        target_inc = app._um_to_inc(app._inc_to_um(reference_count)+step['target_user_um'])
        actual_inc = app.last_position_inc
        if actual_inc is None or abs(actual_inc-target_inc) > app.runtime_position_window_inc:
            raise RuntimeError('Reported completion outside unchanged position window')
        item = dict(step, elapsed_s=time.monotonic()-started, target_inc=target_inc,
                    actual_inc=actual_inc, error_inc=actual_inc-target_inc)
        report['completed'].append(item)
        emit('verified_completion', **item)
        save()

    def done(generation):
        if (app.shortcut_active is not None and app.shortcut_wait_deadline is None
                and int(app.shortcut_active['generation']) == generation):
            complete(batch['steps'][int(app.shortcut_active['number'])-1])
        original_done(generation)
    app._handle_shortcut_done = done

    def step_start(step):
        item = {'index': batch['steps'][int(step['number'])-1]['index'], 'elapsed_s': time.monotonic()-started}
        report['submitted'].append(item)
        emit('submit', **item)
        original_start(step)
    app._start_shortcut_step = step_start

    def start_batch():
        nonlocal batch, state
        batch = plan['batches'][batch_index]
        app.shortcut_dwell_var.set(str(batch['dwell_s'] if hardware else 0))
        if batch['kind'] == 'single':
            step = batch['steps'][0]
            app.position_panel.user_target.set(str(step['target_user_um']))
            app.position_panel.speed.set(str(step['speed_um_s']))
            report['submitted'].append({'index': step['index'], 'elapsed_s': time.monotonic()-started})
        else:
            presets = tuple((i, f"压力步骤 {step['index']}", step['target_user_um'], step['speed_um_s'])
                            for i, step in enumerate(batch['steps'], 1))
            control_gui.SHORTCUT_PRESETS = shortcut_presets.SHORTCUT_PRESETS = presets
            for i, name, target, speed in presets:
                app.position_panel.shortcut_buttons[i].configure(text=f'{name} / {target:g} / {speed:g}')
                app.position_panel.shortcut_buttons[i].invoke()
        app.position_panel.redraw()
        report['start_clicks'] += 1
        app.position_panel.start_button.invoke()
        state = 'moving'
        emit('batch_started', batch_id=batch['id'], kind=batch['kind'])

    def tick():
        nonlocal state, batch_index, next_at, last_tick, max_tick_gap, last_save
        if finished:
            return
        now = time.monotonic()
        max_tick_gap = max(max_tick_gap, now-last_tick)
        last_tick = now
        idle = not app.command_busy and not app.status_busy and not app.stop_pending and app.ui_queue.empty()
        if now-last_save > 5:
            save()
            last_save = now
        if report['errors'] and idle:
            finish('FAILED')
            return
        if state == 'connecting' and app.node is not None and idle and app.position_panel.is_fresh():
            current = app.last_position_inc
            app.position_panel.apply_user_zero(app._inc_to_um(reference_count), now)
            app._set_status({'axis': 'Actuator A', 'position_um': app._inc_to_um(current),
                             'sample_time': now, 'statusword': app.last_statusword,
                             'state': app.state_var.get(), 'mode': app.mode_var.get()})
            app.position_panel.apply_travel()
            report['initial_position_inc'] = current
            if 'initial_fixture_user_um' in plan:
                actual_user = app._inc_to_um(current-reference_count)
                if abs(actual_user-plan['initial_fixture_user_um']) > 5:
                    report['errors'].append('Acceptance fixture must first be verified at user 100 um; no automatic fixture move')
                    finish('FAILED')
                    return
            start_batch()
        elif state == 'moving' and idle and app.active_target_inc is None and app.shortcut_active is None:
            if batch['kind'] == 'single' and app.target_status_var.get().startswith('已到达'):
                complete(batch['steps'][0])
            expected = batch['steps'][-1]['index']
            if len(report['completed']) != expected:
                report['errors'].append(app.target_status_var.get())
                finish('FAILED')
                return
            batch_index += 1
            if batch_index == len(plan['batches']):
                finish('PASSED')
                return
            next_at = now+(batch['dwell_s'] if hardware else 0)
            state = 'waiting'
        elif state == 'waiting' and idle and now >= next_at:
            if app.test_stop_requested.is_set() or app.node is None:
                finish('STOPPED')
                return
            start_batch()
        if state == 'connecting' and now-started > 30:
            report['errors'].append('Connection/preflight timeout')
            finish('FAILED')
            return
        # Application per-move deadlines and STOP stay active. Bound the whole run too.
        if now-started > max(900, plan['estimated_motion_and_dwell_s']*3+plan['count']*10):
            report['errors'].append('Overall stress deadline exceeded')
            finish('TIMEOUT')
            return
        app.after(50, tick)

    if hardware:
        app.after(100, app.connect_device)
    app.after(200, tick)
    app.mainloop()
    if not finished:
        report['result'] = 'CLOSED_BY_USER'
    report['shutdown_completed'] = app.node is None and not app.stop_unconfirmed
    if report['shutdown_completed'] and 33 in verified_stops:
        report['last_ui_position_inc'] = report.get('final_position_inc')
        report['final_position_inc'] = verified_stops[33]['position_inc']
        report['final_statusword'] = verified_stops[33]['statusword']
        report['final_position_source'] = 'verified_stop_before_disconnect'
    if not report['shutdown_completed']:
        report['result'] = 'SHUTDOWN_UNCONFIRMED'
    report['source_unchanged'] = all(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==sha
        for name,sha in report['source_manifest'].items() for p in [ROOT/name])
    if not report['source_unchanged']:
        report['errors'].append('Application sources changed during the run; not a frozen-version acceptance')
        report['result'] = 'SOURCE_CHANGED'
    (output/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=2026110330)
    parser.add_argument('--count', type=int, default=500)
    parser.add_argument('--start-index', type=int, default=1)
    parser.add_argument('--end-index', type=int)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--acceleration', type=float, default=50)
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--hardware', action='store_true')
    parser.add_argument('--plan-file',type=Path)
    parser.add_argument('--reference-count',type=int)
    args = parser.parse_args()
    if args.plan_file:
        from plan_random1000 import generate
        plan=json.loads(args.plan_file.read_text(encoding='utf-8'))
        if plan != generate(plan.get('seed'), 1000):
            parser.error('Acceptance plan must exactly match the frozen 1000-case generator')
        if args.start_index != 1 or args.end_index is not None:
            parser.error('Zero-failure acceptance must run from the beginning; do not resume a failed run')
        if args.hardware and args.reference_count is None:
            parser.error('Supply the operator-confirmed physical reference count for real acceptance')
        if args.acceleration != 50:
            parser.error('This acceptance plan requires unchanged acceleration 50')
    else:
        plan = select_plan_range(generate_plan(args.seed, args.count), args.start_index, args.end_index)
    reference_count=-143 if args.reference_count is None else args.reference_count
    if args.plan_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'count': plan['count'], 'estimated_seconds': plan['estimated_motion_and_dwell_s']}))
    else:
        # A simulation cannot accidentally fall through to real setup_network.
        if args.hardware:
            result = run(plan, args.output, True, args.acceleration, reference_count)
        else:
            with patch('control_gui.setup_network', side_effect=AssertionError('CAN forbidden in simulation')):
                result = run(plan, args.output, False, args.acceleration, reference_count)
        print(json.dumps({k: result.get(k) for k in ('result','completed_count','elapsed_s','errors','shutdown_completed')}, ensure_ascii=True))
        raise SystemExit(0 if result['result'] == 'PASSED' else 1)
