"""Offline two-person annotations and immutable quality reports; grants are external.

No model or machine score is exposed by freeze. report consumes an authenticated
API evaluation export after annotation lock; it never writes readiness evidence.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re

RATINGS={'cold':0,'warm':1,'hot':2,'exclude':None}
def encoded(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(value): return hashlib.sha256(encoded(value)).hexdigest()
def read(path):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    return json.loads(Path(path).read_text(),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('non-finite JSON')))
def save(path,value):
    with Path(path).open('x') as stream: stream.write(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def freeze(source):
    if set(source)!={'schema_version','stage','manifest','source_ref','pairs'} or source['schema_version']!=1 or source['stage'] not in {'direction','qualification'}: raise ValueError('invalid input shape')
    manifest=source['manifest']
    if set(manifest)!={'manifest_id','manifest_sha256','wordbank_manifest_id','calibration_id'} or not re.fullmatch('[0-9a-f]{64}',manifest['manifest_sha256']) or any(not isinstance(v,str) or not v for v in manifest.values()): raise ValueError('frozen joint manifest required')
    if not isinstance(source['source_ref'],str) or not source['source_ref'].strip(): raise ValueError('licensed source reference required')
    if not isinstance(source['pairs'],list) or not 1<=len(source['pairs'])<=20000: raise ValueError('bounded samples required')
    seen=set(); reviewers=set(); rows=[]
    for row in source['pairs']:
        if not {'puzzle_version_id','guess','split','annotations'}<=row.keys() or set(row)-{'puzzle_version_id','guess','split','annotations','adjudication','relation'}: raise ValueError('invalid annotation shape')
        if row['split'] not in {'calibration','blind'} or not isinstance(row['guess'],str) or not 1<=len(row['guess'])<=32: raise ValueError('invalid pair')
        key=(row['puzzle_version_id'],row['guess'])
        if key in seen: raise ValueError('calibration and blind samples must be independent')
        seen.add(key)
        labels=row['annotations']
        if not isinstance(labels,list) or len(labels)!=2 or any(set(a)!={'reviewer_id','rating'} or not isinstance(a['reviewer_id'],str) or not a['reviewer_id'] or a['rating'] not in RATINGS for a in labels) or labels[0]['reviewer_id']==labels[1]['reviewer_id']: raise ValueError('two distinct complete human annotations required')
        reviewers.update(a['reviewer_id'] for a in labels)
        rating=labels[0]['rating']
        if rating!=labels[1]['rating']:
            adj=row.get('adjudication',{})
            if set(adj)!={'reviewer_id','rating','notes'} or not adj['reviewer_id'] or adj['rating'] not in RATINGS or not isinstance(adj['notes'],str) or not adj['notes'].strip(): raise ValueError('human disagreement needs explicit adjudication')
            rating=adj['rating']
        rows.append(row|{'human_rating':rating})
    puzzles=len({r['puzzle_version_id'] for r in rows}); minimum=(20,200) if source['stage']=='direction' else (100,1000)
    complete=puzzles>=minimum[0] and len(rows)>=minimum[1] and {r['split'] for r in rows}=={'calibration','blind'}
    result={'schema_version':1,'stage':source['stage'],'manifest':manifest,'source_ref':source['source_ref'],'annotation_locked':True,'annotation_hash':digest(source),'pairs':rows,'reviewers':sorted(reviewers),'puzzle_count':puzzles,'pair_count':len(rows),'complete':complete,'requirements':{'puzzles':minimum[0],'pairs':minimum[1]},'gate_authority':False}
    return result|{'frozen_hash':digest(result)}
def report(frozen,scored):
    original={k:v for k,v in frozen.items() if k!='frozen_hash'}
    if frozen.get('annotation_locked') is not True or digest(original)!=frozen['frozen_hash']: raise ValueError('annotation lock/hash mismatch')
    if scored.get('manifest')!=frozen['manifest'] or scored.get('annotation_hash')!=frozen['annotation_hash'] or not isinstance(scored.get('producer'),str) or not scored['producer']: raise ValueError('matching API evaluation export required')
    values={}
    for row in scored['pairs']:
        if set(row)!={'puzzle_version_id','guess','raw_similarity','display_score'}: raise ValueError('invalid scored pair fields')
        key=(row['puzzle_version_id'],row['guess'])
        if key in values: raise ValueError('duplicate scored pair')
        if any(isinstance(row.get(k),bool) or not isinstance(row.get(k),(int,float)) or not math.isfinite(row[k]) for k in ('raw_similarity','display_score')) or not -1.001<=row['raw_similarity']<=1.001 or not 0<=row['display_score']<=99.9: raise ValueError('finite actual scores required')
        values[key]=row
    expected={(r['puzzle_version_id'],r['guess']) for r in frozen['pairs']}
    if values.keys()!=expected: raise ValueError('scored sample set must exactly match frozen annotations')
    rows=[r|values[(r['puzzle_version_id'],r['guess'])] for r in frozen['pairs']]
    def metrics(items):
        groups=defaultdict(list); bypuzzle=defaultdict(list)
        for r in items:
            groups[r['human_rating']].append(r); bypuzzle[r['puzzle_version_id']].append(r)
        concordant=total=0
        for items2 in bypuzzle.values():
            for i,a in enumerate(items2):
                for b in items2[i+1:]:
                    ra,rb=RATINGS[a['human_rating']],RATINGS[b['human_rating']]
                    if ra is None or rb is None or ra==rb: continue
                    total+=1; concordant+=int((a['raw_similarity']-b['raw_similarity'])*(ra-rb)>0)
        return {'pairs':len(items),'excluded_pairs':len(groups['exclude']),'pairwise_comparisons':total,'concordant':concordant,'pairwise_accuracy':concordant/total if total else None,'by_rating':{name:{'count':len(group),'raw_mean':sum(r['raw_similarity'] for r in group)/len(group),'display_mean':sum(r['display_score'] for r in group)/len(group)} for name,group in groups.items() if group}}
    splits={name:metrics([r for r in rows if r['split']==name]) for name in ('calibration','blind')}
    acc=splits['blind']['pairwise_accuracy']
    relations={name:metrics([r for r in rows if r.get('relation','unspecified')==name]) for name in sorted({r.get('relation','unspecified') for r in rows})}
    return {'schema_version':1,'frozen_hash':frozen['frozen_hash'],'annotation_hash':frozen['annotation_hash'],'manifest':frozen['manifest'],'producer':scored['producer'],'complete':frozen['complete'],'splits':splits,'by_relation':relations,'all':metrics(rows),'candidate_meets_numeric_threshold':bool(frozen['complete'] and acc is not None and acc>=.9),'gate_authority':False,'requires_release_review':True,'pairs':rows}
def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); subs=parser.add_subparsers(dest='command',required=True)
    f=subs.add_parser('freeze'); f.add_argument('input'); f.add_argument('output')
    r=subs.add_parser('report'); r.add_argument('frozen'); r.add_argument('scores'); r.add_argument('output')
    args=parser.parse_args(argv)
    try:
        output=freeze(read(args.input)) if args.command=='freeze' else report(read(args.frozen),read(args.scores))
        save(args.output,output); print(json.dumps({'status':'written','complete':output['complete'],'gate_authority':False})); return 0
    except (ValueError,KeyError,TypeError,OSError) as error:
        print('QUALITY_INPUT_INVALID: '+str(error),file=__import__('sys').stderr); return 2
if __name__=='__main__': raise SystemExit(main())
