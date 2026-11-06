"""Freeze requested random acceptance inputs; this tool cannot open CAN."""
import argparse
import csv
import hashlib
import json
import random
from pathlib import Path


def generate(seed=2026110434, count=1000):
    rng=random.Random(seed)
    batches=[]
    index=0
    current=100.0
    while index<count:
        size=min(count-index,1 if rng.random()<0.25 else rng.randint(2,7))
        dwell=0.0 if rng.random()<0.3 else rng.choice((0.2,0.5,1.0,3.0,30.0)) if rng.random()<0.3 else round(rng.uniform(0.2,30),2)
        batch={'id':len(batches)+1,'kind':'single' if size==1 else 'sequence','dwell_s':dwell,'steps':[]}
        for _ in range(size):
            target=rng.randint(100,12000)
            speed=rng.randint(2,500)
            index+=1
            batch['steps'].append({'index':index,'target_user_um':target,'speed_um_s':speed,
                'planned_distance_um':abs(target-current),'category':'independent_uniform_target_and_speed'})
            current=target
        batches.append(batch)
    estimate=sum(s['planned_distance_um']/s['speed_um_s'] for b in batches for s in b['steps'])
    estimate+=sum(b['dwell_s']*(len(b['steps'])-1+(i<len(batches)-1)) for i,b in enumerate(batches))
    return {'seed':seed,'count':count,'target_range_um':[100,12000],'speed_range_um_s':[2,500],
        'sampling':'Independent uniform integer user targets (um) and speeds (um/s); random single/sequence grouping and dwell',
        'acceleration_um_s2':50,'initial_fixture_user_um':100,'continuous_trajectory':True,
        'estimated_motion_and_dwell_s':estimate,'estimate_excludes':'acceleration ramps, jerk, I/O, arrival and disable verification',
        'acceptance':{'hardware_required':True,'single_final_source_required':True,'completed_count':count,
                      'allowed_failures':0,'retry_replaces_failure':False,'resume_after_failure_can_pass':False},
        'hardware_start_gate':'Blocked until enable jump, long slow plateau and overshoot are resolved and physically revalidated',
        'batches':batches}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        parser.error('Do not overwrite a frozen plan')
    args.output.mkdir(parents=True)
    plan=generate()
    path=args.output/'plan.json'
    path.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    with (args.output/'cases.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['index','batch_id','kind','dwell_s','target_user_um','speed_um_s','planned_distance_um'])
        writer.writeheader()
        for b in plan['batches']:
            for s in b['steps']:
                writer.writerow(dict(index=s['index'],batch_id=b['id'],kind=b['kind'],dwell_s=b['dwell_s'],
                    target_user_um=s['target_user_um'],speed_um_s=s['speed_um_s'],planned_distance_um=s['planned_distance_um']))
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    (args.output/'plan.sha256').write_text(sha+'  plan.json\n',encoding='ascii')
    print(json.dumps({'count':plan['count'],'batches':len(plan['batches']),
        'nominal_hours_excluding_ramps_and_checks':plan['estimated_motion_and_dwell_s']/3600,
        'sha256':sha,'hardware_started':False}))


if __name__=='__main__':
    main()
