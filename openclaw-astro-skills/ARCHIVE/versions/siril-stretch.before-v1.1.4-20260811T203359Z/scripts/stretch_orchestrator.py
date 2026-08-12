#!/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python
from __future__ import annotations
import argparse,hashlib,json,os,shutil,sys,time
from pathlib import Path
from typing import Any
import stretch_engine as eng

VERSION="1.1.3"
WORKSPACE=Path("/home/peter/.openclaw/workspace/agents/codewarrior")
MIN_ROUNDS=2; MAX_ROUNDS=5

class OrchestrationError(RuntimeError): pass

def sha(path:Path)->str: return eng.sha256_file(path)
def emit(x:dict[str,Any],code:int=0)->int: print(json.dumps(x,indent=2,sort_keys=True)); return code

def paths(name:str)->dict[str,Path]:
    project=WORKSPACE/'Projects'/name
    return {"project":project,"source":project/'processing/sho-channel-balance/SHO-starless-linear-balanced.fit',
            "source_manifest":project/'processing/sho-channel-balance/sho-channel-balance-manifest.json',
            "runtime":project/'.siril-stretch',"active":project/'.siril-stretch/active.json',
            "fresh":project/'.siril-stretch/fresh-authorization.json',"stable":project/'processing/stretch',
            "stable_fit":project/'processing/stretch/SHO-starless-stretched.fit',
            "stable_manifest":project/'processing/stretch/stretch-manifest.json',
            "stable_review":project/'processing/stretch/visual-selection-record.json'}

def load_json(p:Path)->dict[str,Any]:
    try: x=json.loads(p.read_text(encoding='utf-8'))
    except Exception as e: raise OrchestrationError(f"Cannot read required JSON {p}: {e}") from e
    if not isinstance(x,dict): raise OrchestrationError(f"Required JSON is not an object: {p}")
    return x

def validate_project(name:str)->tuple[dict[str,Path],dict[str,Any]]:
    p=paths(name)
    if not p['project'].is_dir(): raise OrchestrationError(f"Project does not exist: {p['project']}")
    if not p['source'].is_file(): raise OrchestrationError(f"Exact channel-balance source missing: {p['source']}")
    if not p['source_manifest'].is_file(): raise OrchestrationError(f"Exact channel-balance manifest missing: {p['source_manifest']}")
    m=load_json(p['source_manifest']); errors=[]
    if m.get('status')!='ready': errors.append('channel-balance status is not ready')
    if m.get('visual_review_completed') is not True: errors.append('channel-balance visual review incomplete')
    recorded=(m.get('output') or m.get('balanced_starless') or m.get('linear_starless') or {}).get('sha256')
    if recorded and recorded!=sha(p['source']): errors.append('channel-balance FITS checksum differs from manifest')
    if errors: raise OrchestrationError('Upstream channel-balance contract failed: '+'; '.join(errors))
    return p,m

def canonical_status(p:dict[str,Path])->dict[str,Any]:
    if not p['stable_manifest'].is_file(): return {"status":"missing"}
    try: m=load_json(p['stable_manifest'])
    except Exception as e: return {"status":"invalid","error":str(e)}
    if not p['stable_fit'].is_file(): return {"status":"invalid","error":"canonical stretch FITS missing"}
    errors=[]
    if m.get('orchestration_version')!=VERSION: errors.append('canonical stretch policy/version obsolete')
    if (m.get('output') or {}).get('sha256')!=sha(p['stable_fit']): errors.append('canonical FITS checksum mismatch')
    if m.get('visual_review_completed') is not True: errors.append('canonical visual review incomplete')
    return {"status":"ready" if not errors else "obsolete","errors":errors,"manifest":m,
            "fit_sha256":sha(p['stable_fit']),"manifest_sha256":sha(p['stable_manifest'])}

def save_active(p:dict[str,Path],state:dict[str,Any])->None: eng.atomic_json(p['active'],state)
def load_active(p:dict[str,Path])->dict[str,Any]|None: return load_json(p['active']) if p['active'].is_file() else None

def review_plan(state:dict[str,Any],round_result:dict[str,Any])->dict[str,Any]:
    cands=round_result['candidates']
    return {"status":"visual_review_required","review_scope":"round","orchestration_version":VERSION,
            "project_name":state['project_name'],"run_root":state['run_root'],"round":round_result['round'],
            "recommended_candidate":round_result['recommended_candidate'],"publication_eligible_candidates":round_result['eligible_candidates'],
            "read_targets":[{"candidate":c['candidate'],"path":c['preview']['path'],"sha256":c['preview']['sha256']} for c in cands],
            "required_review_fields":["brightness","contrast","color","structure","background","highlights"],
            "read_target_policy":{"path_handling":"verbatim","directory_discovery_forbidden":True,
               "forbidden_recovery_tools":["ls","find","grep","jq","globbing"],"on_read_failure":"stop_and_report_exact_failed_path"},
            "selection_rule":"Choose the technically eligible candidate with the best balance of brightness, nebular contrast, color richness, preserved faint structure, acceptable background, and safe highlights. Histogram placement targets are guides only.",
            "minimum_rounds":MIN_ROUNDS,"maximum_rounds":MAX_ROUNDS,
            "instruction":"Read every read_targets[].path verbatim, compare all three, then call select-round with notes for all compared candidates. Round 1 must continue; from round 2 onward continue only if another GHS+BP round is likely to improve the image safely."}

def final_panel(state:dict[str,Any])->dict[str,Any]:
    winners=state['round_winners']; choices=[]
    # Best three round winners when available.
    ranked=sorted(winners,key=lambda x:x['technical_score'],reverse=True)
    for x in ranked[:3]: choices.append(x)
    # With only two rounds, add the best non-winning candidate from the final round.
    if len(choices)<3:
        final_round=load_json(Path(state['run_root'])/f"round-{state['current_round']:02d}"/'round-result.json')
        existing={(x['round'],x['candidate']) for x in choices}
        extras=[]
        for c in final_round['candidates']:
            if c['eligible'] and (c['round'],c['candidate']) not in existing:
                extras.append({"round":c['round'],"candidate":c['candidate'],"technical_score":c['technical_score'],
                               "output":c['output'],"preview":c['preview']})
        extras.sort(key=lambda x:x['technical_score'],reverse=True); choices.extend(extras[:3-len(choices)])
    if len(choices)<3: raise OrchestrationError('Could not assemble three final eligible stretch candidates.')
    fr=Path(state['run_root'])/'final-review'; fr.mkdir(exist_ok=True)
    panel=[]
    for i,x in enumerate(choices[:3]):
        fid=f"final-candidate-{i:02d}"; srcfit=Path(x['output']['path']); srcpng=Path(x['preview']['path'])
        fit=fr/f"{fid}.fit"; png=fr/f"{fid}.png"
        if not fit.exists(): shutil.copy2(srcfit,fit)
        if not png.exists(): shutil.copy2(srcpng,png)
        panel.append({"candidate":fid,"source_round":x['round'],"source_candidate":x['candidate'],"technical_score":x['technical_score'],
                      "output":{"path":str(fit),"sha256":sha(fit)},"preview":{"path":str(png),"sha256":sha(png)}})
    state['final_candidates']=panel; state['status']='awaiting_final_review'; save_active(paths(state['project_name']),state)
    return {"status":"visual_review_required","review_scope":"final","orchestration_version":VERSION,"project_name":state['project_name'],
            "run_root":state['run_root'],"read_targets":[{"candidate":x['candidate'],"path":x['preview']['path'],"sha256":x['preview']['sha256']} for x in panel],
            "required_review_fields":["brightness","contrast","color","structure","background","highlights","overall_balance"],
            "read_target_policy":{"path_handling":"verbatim","directory_discovery_forbidden":True,
              "forbidden_recovery_tools":["ls","find","grep","jq","globbing"],"on_read_failure":"stop_and_report_exact_failed_path"},
            "selection_rule":"Select the final candidate with the best overall balance of appropriate brightness, strong contrast, rich retained color, preserved structure, acceptable background and no clipping. A later round does not automatically win.",
            "instruction":"Read all three final read targets verbatim, compare them, then call select-publish with the selected final candidate and notes for all three."}

def new_run(name:str,p:dict[str,Path],timeout:int)->dict[str,Any]:
    p['runtime'].mkdir(parents=True,exist_ok=True)
    rid=time.strftime('stretch-%Y%m%dT%H%M%SZ',time.gmtime())+'-'+sha(p['source'])[:8]
    root=p['runtime']/rid; root.mkdir(exist_ok=False)
    rr=eng.run_round(p['source'],root,1,timeout)
    state={"schema_version":1,"orchestration_version":VERSION,"project_name":name,"run_root":str(root),"source_path":str(p['source']),
           "source_sha256":sha(p['source']),"source_manifest_path":str(p['source_manifest']),"source_manifest_sha256":sha(p['source_manifest']),
           "status":"awaiting_round_review","current_round":1,"round_winners":[],"final_candidates":[],"canonical_output_changed":False}
    eng.atomic_json(root/'run-manifest.json',state); save_active(p,state); return review_plan(state,rr)

def command_advance(a)->int:
    p,_=validate_project(a.project); c=canonical_status(p); active=load_active(p)
    if active:
        if active.get('source_sha256')!=sha(p['source']): raise OrchestrationError('Active stretch run belongs to an older channel-balance source; preserve it and start only after explicit recovery.')
        if active['status']=='awaiting_round_review':
            rr=load_json(Path(active['run_root'])/f"round-{active['current_round']:02d}"/'round-result.json'); return emit(review_plan(active,rr))
        if active['status']=='awaiting_final_review': return emit(final_panel(active))
        raise OrchestrationError(f"Unsupported active run state: {active['status']}")
    if c['status'] in ('ready','obsolete'):
        auth=load_json(p['fresh']) if p['fresh'].is_file() else None
        if not auth or auth.get('source_sha256')!=sha(p['source']):
            return emit({"status":"confirmation_required","action":"await_user_confirmation","project":str(p['project']),
              "question":f"Stretch for {a.project} has already completed successfully. Do you want me to run it again as a fresh run?",
              "current_canonical_status":c['status'],"current_canonical_output_sha256":c.get('fit_sha256'),"production_processing_started":False})
        p['fresh'].unlink(missing_ok=True)
    if a.plan_only:
        return emit({"status":"would_start_round","project":str(p['project']),"source":str(p['source']),"source_sha256":sha(p['source']),
          "minimum_rounds":MIN_ROUNDS,"maximum_rounds":MAX_ROUNDS,"production_processing_started":False})
    return emit(new_run(a.project,p,a.timeout))

def command_confirm(a)->int:
    p,_=validate_project(a.project); c=canonical_status(p)
    if c['status'] not in ('ready','obsolete'): raise OrchestrationError('No completed canonical stretch exists that requires fresh-run authorization.')
    p['runtime'].mkdir(parents=True,exist_ok=True)
    eng.atomic_json(p['fresh'],{"project_name":a.project,"source_sha256":sha(p['source']),"canonical_manifest_sha256":c.get('manifest_sha256'),"authorized_at":time.time()})
    return emit({"status":"fresh_run_authorized","project":a.project,"next_action":"advance"})

def notes(values:list[str],expected:list[str])->dict[str,str]:
    out={}
    for raw in values:
        if '=' not in raw: raise OrchestrationError('Each --note must be candidate=structured observation.')
        k,v=raw.split('=',1); k=k.strip(); v=' '.join(v.split())
        if k not in expected: raise OrchestrationError(f'Unexpected candidate note: {k}')
        if k in out: raise OrchestrationError(f'Duplicate candidate note: {k}')
        required_labels=['brightness:','contrast:','color:','structure:','background:','highlights:']
        lower=v.lower()
        missing=[label for label in required_labels if label not in lower]
        if missing: raise OrchestrationError(f'Visual note for {k} is missing required fields: {missing}')
        if len(v)<120: raise OrchestrationError(f'Visual note for {k} is too vague; give specific observations for every required field.')
        out[k]=v
    if set(out)!=set(expected): raise OrchestrationError(f'Notes must cover every compared candidate exactly: {expected}')
    return out

def command_select_round(a)->int:
    p,_=validate_project(a.project); state=load_active(p)
    if not state: raise OrchestrationError('No active stretch run.')
    if Path(a.run_root).resolve()!=Path(state['run_root']).resolve(): raise OrchestrationError('Run root does not match active run.')
    if state['status']!='awaiting_round_review': raise OrchestrationError('Active run is not awaiting a round review.')
    rr=load_json(Path(state['run_root'])/f"round-{state['current_round']:02d}"/'round-result.json')
    compared=a.compared; expected=[c['candidate'] for c in rr['candidates']]
    if compared!=expected: raise OrchestrationError(f'--compared must repeat exact candidate order: {expected}')
    if a.candidate not in rr['eligible_candidates']: raise OrchestrationError('Selected round candidate is not technically eligible.')
    n=notes(a.note,expected); selected=next(c for c in rr['candidates'] if c['candidate']==a.candidate)
    record={"round":state['current_round'],"selected_candidate":a.candidate,"compared":compared,"notes":n,"selected_output":selected['output'],
            "selected_preview":selected['preview'],"technical_score":selected['technical_score'],"continue_requested":a.continue_round=='yes'}
    eng.atomic_json(Path(state['run_root'])/f"round-{state['current_round']:02d}"/'visual-selection-record.json',record)
    state['round_winners'].append({"round":state['current_round'],"candidate":a.candidate,"technical_score":selected['technical_score'],
                                   "output":selected['output'],"preview":selected['preview']})
    must_continue=state['current_round']<MIN_ROUNDS
    continue_now=must_continue or a.continue_round=='yes'
    if state['current_round']>=MAX_ROUNDS: continue_now=False
    if continue_now:
        nr=state['current_round']+1; rr2=eng.run_round(Path(selected['output']['path']),Path(state['run_root']),nr,a.timeout)
        state['current_round']=nr; state['status']='awaiting_round_review'; save_active(p,state); eng.atomic_json(Path(state['run_root'])/'run-manifest.json',state)
        return emit(review_plan(state,rr2))
    state['status']='building_final_review'; save_active(p,state); eng.atomic_json(Path(state['run_root'])/'run-manifest.json',state)
    return emit(final_panel(state))

def command_select_publish(a)->int:
    p,_=validate_project(a.project); state=load_active(p)
    if not state: raise OrchestrationError('No active stretch run.')
    if Path(a.run_root).resolve()!=Path(state['run_root']).resolve(): raise OrchestrationError('Run root mismatch.')
    if state['status']!='awaiting_final_review': raise OrchestrationError('Active run is not awaiting final review.')
    expected=[x['candidate'] for x in state['final_candidates']]
    if a.compared!=expected: raise OrchestrationError(f'--compared must repeat exact final candidate order: {expected}')
    if a.candidate not in expected: raise OrchestrationError('Selected final candidate is not in the exact final panel.')
    n=notes(a.note,expected); sel=next(x for x in state['final_candidates'] if x['candidate']==a.candidate)
    review={"schema_version":1,"project":a.project,"run_root":state['run_root'],"visual_review_completed":True,"selected_candidate":a.candidate,
            "selected_source_round":sel['source_round'],"selected_source_candidate":sel['source_candidate'],"compared":a.compared,"notes":n,"reviewed_at":time.time()}
    review_path=Path(state['run_root'])/'final-review'/'visual-selection-record.json'; eng.atomic_json(review_path,review)
    staging=Path(state['run_root'])/'publish-staging'
    if staging.exists(): shutil.rmtree(staging)
    staging.mkdir(); out=staging/'SHO-starless-stretched.fit'; shutil.copy2(Path(sel['output']['path']),out)
    shutil.copy2(review_path,staging/'visual-selection-record.json')
    manifest={"schema_version":1,"status":"ready","orchestration_version":VERSION,"processing_engine_version":eng.VERSION,"project":a.project,
      "stage_order":{"upstream":"siril-sho-channel-balance","current":"siril-stretch","downstream":"siril-green-reduction"},
      "source_contract_revision":"iterative-ghs-bp-v1","next_stage":"siril-green-reduction","green_reduction_permitted":True,
      "ghs_pass1_permitted":False,"ghs_pass2_permitted":False,"standalone_black_point_permitted":False,"starless_processing_permitted":True,
      "source":{"path":state['source_path'],"sha256":state['source_sha256'],"manifest_path":state['source_manifest_path'],"manifest_sha256":state['source_manifest_sha256']},
      "round_count":state['current_round'],"round_winners":state['round_winners'],"selected":{"candidate":a.candidate,"source_round":sel['source_round'],"source_candidate":sel['source_candidate']},
      "output":{"path":str(p['stable_fit']),"sha256":sha(out)},"visual_review_completed":True,"visual_review":{"path":str(p['stable_review'])}}
    eng.atomic_json(staging/'stretch-manifest.json',manifest)
    # Fully validate staging before replacing an existing canonical result.
    if sha(out)!=manifest['output']['sha256']: raise OrchestrationError('Publish staging checksum failed.')
    backup=None
    if p['stable'].exists():
        backup=p['stable'].with_name('stretch.previous-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
        os.replace(p['stable'],backup)
    try: os.replace(staging,p['stable'])
    except Exception:
        if backup is not None and backup.exists() and not p['stable'].exists(): os.replace(backup,p['stable'])
        raise
    final=canonical_status(p)
    if final['status']!='ready': raise OrchestrationError(f'Post-publication validation failed: {final}')
    state['status']='published'; state['canonical_output_changed']=True; eng.atomic_json(Path(state['run_root'])/'run-manifest.json',state)
    p['active'].unlink(missing_ok=True)
    return emit({"status":"ready","orchestration_version":VERSION,"project":a.project,"round_count":state['current_round'],
      "selected_candidate":a.candidate,"canonical_output":str(p['stable_fit']),"canonical_output_sha256":sha(p['stable_fit']),
      "canonical_manifest":str(p['stable_manifest']),"next_stage":"siril-green-reduction","green_reduction_permitted":True,
      "previous_canonical_preserved_at":str(backup) if backup else None})

def command_status(a)->int:
    p,_=validate_project(a.project); active=load_active(p); c=canonical_status(p)
    return emit({"status":"active" if active else c['status'],"orchestration_version":VERSION,"project":str(p['project']),"active_run":active,"canonical":c})

def command_self_test(a)->int:
    root=Path(a.root) if a.root else Path('/tmp')/f"siril-stretch-selftest-{os.getpid()}"
    if root.exists(): shutil.rmtree(root)
    result=eng.self_test(root,a.timeout)
    result.update({"orchestration_version":VERSION,"minimum_rounds":MIN_ROUNDS,"maximum_rounds":MAX_ROUNDS,"bounded_round_candidates":3,
      "exact_read_targets_required":True,"directory_discovery_forbidden":True,"completed_stage_confirmation_preserved":True,
      "final_three_candidate_review":True,"downstream":"siril-green-reduction"})
    if not a.keep: shutil.rmtree(root,ignore_errors=True)
    return emit(result)

def parser():
    p=argparse.ArgumentParser(description='Autonomous iterative GHS + BP stretch stage.'); p.add_argument('--version',action='version',version=VERSION)
    s=p.add_subparsers(dest='command',required=True)
    x=s.add_parser('advance'); x.add_argument('--project',required=True); x.add_argument('--timeout',type=int,default=1800); x.add_argument('--plan-only',action='store_true'); x.set_defaults(func=command_advance)
    x=s.add_parser('confirm-fresh'); x.add_argument('--project',required=True); x.set_defaults(func=command_confirm)
    x=s.add_parser('select-round'); x.add_argument('--project',required=True); x.add_argument('--run-root',required=True); x.add_argument('--candidate',required=True); x.add_argument('--compared',action='append',required=True); x.add_argument('--continue',dest='continue_round',choices=['yes','no'],required=True); x.add_argument('--note',action='append',required=True); x.add_argument('--timeout',type=int,default=1800); x.set_defaults(func=command_select_round)
    x=s.add_parser('select-publish'); x.add_argument('--project',required=True); x.add_argument('--run-root',required=True); x.add_argument('--candidate',required=True); x.add_argument('--compared',action='append',required=True); x.add_argument('--note',action='append',required=True); x.set_defaults(func=command_select_publish)
    x=s.add_parser('stage-status'); x.add_argument('--project',required=True); x.set_defaults(func=command_status)
    x=s.add_parser('self-test'); x.add_argument('--root'); x.add_argument('--timeout',type=int,default=600); x.add_argument('--keep',action='store_true'); x.set_defaults(func=command_self_test)
    return p

def main()->int:
    a=parser().parse_args()
    try: return int(a.func(a))
    except (OrchestrationError,eng.StretchError) as e: return emit({"status":"blocked","orchestration_version":VERSION,"error":str(e),"action":"stop_no_discovery"},2)

if __name__=='__main__': raise SystemExit(main())
