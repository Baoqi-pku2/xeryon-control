"""Combine manually reviewed stress batches without erasing failed cases."""
import argparse
import collections
import csv
import json
from pathlib import Path


def summarize(audit):
    authority = json.loads((audit/'hardware500_stage1/plan.json').read_text(encoding='utf-8'))
    cases = {s['index']:dict(s, outcome='NOT_RUN') for b in authority['batches'] for s in b['steps']}
    problems, stages, sources, dwells, command_times, stop_times = [], [], set(), [], [], []
    for folder in sorted((p for p in audit.glob('hardware500_stage*') if p.is_dir()),
                         key=lambda p:int(p.name.split('stage')[-1])):
        if not folder.is_dir() or not (folder/'result.json').exists():
            continue
        report = json.loads((folder/'result.json').read_text(encoding='utf-8'))
        plan = json.loads((folder/'plan.json').read_text(encoding='utf-8'))
        local = {s['index']:s for b in plan['batches'] for s in b['steps']}
        sources.add(report['source_sha256'])
        if not report['hardware_motion']:
            problems.append(f'{folder.name}: simulated data in hardware campaign')
        if not report['shutdown_completed']:
            problems.append(f'{folder.name}: stop/disconnect unconfirmed')
        traces=[]
        for path in (folder/'XeryonControl/results').glob('motion_*.jsonl'):
            rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            commands=[r for r in rows if r['event']=='command']
            if len(commands)!=1:
                problems.append(f'{path.name}: not exactly one continuous command')
            if commands:
                traces.append((commands[0],rows,path.relative_to(audit).as_posix()))
        traces.sort(key=lambda item:item[0]['monotonic_s'])
        previous_stop = None
        for command, rows, path in traces:
            command_times.append(command['time'])
            if previous_stop is not None and command['monotonic_s'] < previous_stop:
                problems.append(f'{path}: next command before verified stop')
            stops = [row for row in rows if row['event']=='stop_verified']
            if stops:
                previous_stop = stops[-1]['monotonic_s']
                stop_times.append(stops[-1]['time'])
                if (stops[-1]['statusword'] & 0x6F) == 0x27:
                    problems.append(f'{path}: drive still enabled after stop')
            starts = [row for row in rows if row['event']=='sequence_dwell_started']
            ends = [row for row in rows if row['event']=='sequence_dwell_finished']
            for start, end in zip(starts, ends):
                elapsed = end['monotonic_s']-start['monotonic_s']
                dwells.append({'requested_s':start['seconds'],'actual_s':elapsed,'trace':path})
                if elapsed < start['seconds']:
                    problems.append(f'{path}: dwell shorter than requested')
            if len(starts)!=len(ends):
                problems.append(f'{path}: incomplete sequence dwell')
        completed={s['index']:s for s in report['completed']}
        submitted=report['submitted']
        if len(traces)>len(submitted):
            problems.append(f'{folder.name}: extra motion commands')
        for offset, submission in enumerate(submitted):
            number=submission['index']
            step=local[number]
            original=step.get('original_index',number)
            case=cases[original]
            if case['outcome']!='NOT_RUN':
                problems.append(f'Case {original}: repeated; original outcome retained')
                continue
            if (step['target_user_um'],step['speed_um_s'])!=(case['target_user_um'],case['speed_um_s']):
                problems.append(f'Case {original}: original input changed')
            case.update(stage=folder.name,source_sha256=report['source_sha256'],
                        outcome='PASSED' if number in completed else 'FAILED',
                        controller_command_recorded=offset<len(traces))
            if number not in completed:
                case['reported_error']=report['errors'] or report['target_status']
            if offset>=len(traces):
                if number in completed:
                    problems.append(f'Case {original}: claimed pass without trace')
                continue
            command,rows,path=traces[offset]
            case['trace']=path
            resolution=command['um_per_count']
            target=round((-143*resolution+step['target_user_um'])/resolution)
            case['target_inc']=target
            case['start_inc']=command['start_inc']
            if command['target_inc']!=target or command['profile']['velocity']!=step['speed_um_s']:
                problems.append(f'Case {original}: commanded target/speed mismatch')
            if command['profile']['acceleration']!=50 or command['profile'].get('jerk')!=500:
                problems.append(f'Case {original}: unexpected trajectory parameters')
            if any(r['event'] in ('same_target_refresh','diagnostic_buffered_target') for r in rows):
                problems.append(f'Case {original}: discarded experimental strategy used')
            reached=[r for r in rows if r['event']=='target_reached']
            stops=[r for r in rows if r['event']=='stop_verified']
            failures=[r for r in rows if r['event'] in ('stop','communication_fault','communication_stop',
                                                       'disable_failed','position_reference_changed')]
            if failures:
                case['trace_failures']=failures
            if stops:
                actual=stops[-1]['position_inc']
                case.update(stopped_inc=actual, stopped_user_um=(actual+143)*resolution,
                            error_to_quantized_target_um=(actual-target)*resolution,
                            error_to_requested_target_um=(actual+143)*resolution-step['target_user_um'])
            if number in completed:
                valid=(len(reached)==1 and bool(stops) and not failures
                       and bool(reached[0]['statusword'] & (1<<10))
                       and abs(stops[-1]['position_inc']-target)<=command['profile']['position_window_inc'])
                if not valid:
                    problems.append(f'Case {original}: arrival/disable evidence does not support pass')
        stages.append({'folder':folder.name,'source_sha256':report['source_sha256'],
                       'result':report['result'],'completed':len(completed),
                       'submitted':len(submitted),'motion_commands':len(traces),'elapsed_s':report['elapsed_s'],
                       'ui_tick_max_gap_s':report['ui_tick_max_gap_s'],
                       'shutdown_completed':report['shutdown_completed'], 'errors':report['errors']})
    counts=collections.Counter(c['outcome'] for c in cases.values())
    velocity={}
    versions={}
    for case in cases.values():
        key=str(case['speed_um_s'])
        velocity.setdefault(key,collections.Counter())[case['outcome']]+=1
        if 'source_sha256' in case:
            versions.setdefault(case['source_sha256'],collections.Counter())[case['outcome']]+=1
    successful=[c for c in cases.values() if c['outcome']=='PASSED']
    report={'planned_count':len(cases),'counts':dict(counts),'speed_outcomes':{k:dict(v) for k,v in velocity.items()},
            'coverage_complete':counts['NOT_RUN']==0,'evidence_consistent':not problems,
            'unrestricted_stable_acceptance':not problems and counts['PASSED']==len(cases)
                and len(sources)==1 and all(stage['result']=='PASSED' for stage in stages),
            'problems':problems,'source_hashes':sorted(sources),
            'source_outcomes':{key:dict(value) for key,value in versions.items()},'stages':stages,
            'max_ui_tick_gap_s':max((stage['ui_tick_max_gap_s'] for stage in stages),default=None),
            'longest_stage_passed_count':max((stage['completed'] for stage in stages),default=0),
            'first_command_time':min(command_times,default=None),
            'last_verified_stop_time':max(stop_times,default=None),
            'total_stage_elapsed_s':sum(stage['elapsed_s'] for stage in stages),
            'max_passed_position_error_um':max((abs(c.get('error_to_requested_target_um',0)) for c in successful),default=None),
            'sequence_dwells':len(dwells),
            'thirty_second_dwells':[item for item in dwells if item['requested_s']==30],
            'cases':list(cases.values())}
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audit',type=Path)
    args=parser.parse_args()
    report=summarize(args.audit)
    (args.audit/'hardware500_summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    fields=['index','outcome','target_user_um','speed_um_s','category','start_inc','target_inc',
            'stopped_inc','stopped_user_um','error_to_requested_target_um','stage','trace','reported_error']
    with (args.audit/'hardware500_cases.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(report['cases'])
    print(json.dumps({k:v for k,v in report.items() if k not in ('cases','thirty_second_dwells')},ensure_ascii=True,indent=2))
    raise SystemExit(0 if report['coverage_complete'] and report['evidence_consistent'] else 1)
