#!/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python
from __future__ import annotations
import argparse,hashlib,json,os,shutil,sys,time
from pathlib import Path
from typing import Any
import stretch_engine as eng

VERSION="1.2.4"
WORKSPACE=Path("/home/peter/.openclaw/workspace/agents/codewarrior")
MIN_ROUNDS=2; MAX_ROUNDS=5

# User-approved M16 manual stretch: advisory calibration evidence only.
# It is never a hard runtime target and is exposed only when the exact upstream
# source checksum matches this M16 data set.
M16_SOURCE_SHA="fac08787a064892106f443612893a819dc1068dda5211271e4f9dbccb0c07577"
M16_MANUAL_REFERENCE={
    "hard_runtime_target":False,
    "reference":"user successful manual iterative Siril GHS/BP stretch",
    "fits_sha256":"edef121ed0b80a9abd84229eee34c68a4964f05086f8483ac28f7fac71925822",
    "history_summary":"4 GHS applications with three BP shifts (0.14, 0.10, 0.02); broad locality approximately -0.43..+0.59",
    "metrics":{"mode":0.2011007871283478,"median":0.20955315728982288,"p95":0.3120945915579796,
               "p99":0.46810175140698745,"p99.9":0.5665558298726876,"maximum":0.8935615420341492,
               "saturation_median":0.3218716651030429,"saturation_p75":0.450798039940319,
               "chroma_median":0.08215704560279846,"chroma_p75":0.12452752143144608,
               "color_contrast_index":0.3920477227},
    "interpretation":"Use only as M16 calibration context. Do not force another target to these values."
}

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

def active_version(state:dict[str,Any])->str:
    v=str(state.get('orchestration_version') or '')
    if v != VERSION:
        raise OrchestrationError(f"Active stretch run version {v!r} is not resume-compatible with processing policy {VERSION}; preserve it and start a fresh run only after explicit recovery/authorization.")
    return v

def calibration_reference(state:dict[str,Any])->dict[str,Any]|None:
    return M16_MANUAL_REFERENCE if state.get('source_sha256')==M16_SOURCE_SHA else None

def compact_metrics(c:dict[str,Any])->dict[str,Any]:
    m=c.get('metrics') or {}
    g=c.get('ghs') or {}
    return {
        "candidate":c.get('candidate'),"technical_score":c.get('technical_score'),
        "round_relative_color_score":c.get('round_relative_color_score'),
        "ghs":{"D":g.get('D'),"B":g.get('B'),"SP":g.get('SP'),"LP":g.get('LP'),"HP":g.get('HP'),"color_mode":g.get('color_mode')},
        "bp":c.get('bp'),"bp_target_floor":c.get('bp_target_floor'),
        "histogram":{"mode":m.get('mode'),"median":m.get('p50'),"p95":m.get('p95'),"p99":m.get('p99'),"p99.9":m.get('p99.9'),"maximum":m.get('maximum')},
        "color":{"saturation_median":m.get('saturation_median'),"saturation_p75":m.get('saturation_p75'),
                 "chroma_median":m.get('chroma_median'),"chroma_p75":m.get('chroma_p75'),
                 "color_contrast_index":m.get('color_contrast_index'),"color_contrast_p75":m.get('color_contrast_p75'),
                 "saturation_retention":m.get('saturation_retention'),"chroma_retention":m.get('chroma_retention'),
                 "color_contrast_retention":m.get('color_contrast_retention')},
        "clipping":{"new_low":m.get('new_low_clip_fraction'),"new_high":m.get('new_high_clip_fraction')}
    }

def reviewable_round_candidates(round_result:dict[str,Any])->tuple[list[dict[str,Any]],list[dict[str,Any]]]:
    cands=round_result.get('candidates')
    eligible=round_result.get('eligible_candidates')
    if not isinstance(cands,list) or not isinstance(eligible,list):
        raise OrchestrationError('Round result is missing candidate/eligibility lists.')
    by_id={c.get('candidate'):c for c in cands if isinstance(c,dict)}
    if len(by_id)!=len(cands): raise OrchestrationError('Round result contains malformed or duplicate candidate IDs.')
    reviewable=[]
    for cid in eligible:
        c=by_id.get(cid)
        if c is None: raise OrchestrationError(f'Technically eligible candidate is missing from round result: {cid}')
        preview=c.get('preview'); output=c.get('output')
        if not isinstance(preview,dict) or not preview.get('path') or not preview.get('sha256'):
            raise OrchestrationError(f'Technically eligible candidate has no valid preview: {cid}')
        if not isinstance(output,dict) or not output.get('path') or not output.get('sha256'):
            raise OrchestrationError(f'Technically eligible candidate has no valid output: {cid}')
        reviewable.append(c)
    if not reviewable: raise OrchestrationError('Round has no technically eligible candidates to review.')
    rejected=[]
    eligible_set=set(eligible)
    for c in cands:
        if c['candidate'] not in eligible_set:
            rejected.append({"candidate":c['candidate'],"rejection_reasons":c.get('rejection_reasons') or [],
                             "preview_available":isinstance(c.get('preview'),dict),"output_available":isinstance(c.get('output'),dict)})
    return reviewable,rejected

def continuation_advisory(state:dict[str,Any],round_result:dict[str,Any],reviewable:list[dict[str,Any]])->dict[str,Any]:
    # Generic signal plus an M16 calibration hint.  This is advisory, never a hard target.
    best=max(reviewable,key=lambda c:c.get('technical_score',-1e9))
    m=best.get('metrics') or {}
    out={
      "recommended_candidate":best.get('candidate'),
      "median":m.get('p50'),"p99_9":m.get('p99.9'),
      "saturation_median":m.get('saturation_median'),"chroma_median":m.get('chroma_median'),
      "color_contrast_index":m.get('color_contrast_index'),
      "guidance":"From round 2 onward, stop when another cycle would mainly increase luminance or upper-tail placement without a material gain in visible colour separation or structure."
    }
    if state.get('source_sha256')==M16_SOURCE_SHA:
      out["m16_calibration_hint"]={
        "manual_reference_is_advisory":True,
        "manual_median":M16_MANUAL_REFERENCE['metrics']['median'],
        "manual_p99_9":M16_MANUAL_REFERENCE['metrics']['p99.9'],
        "manual_saturation_median":M16_MANUAL_REFERENCE['metrics']['saturation_median'],
        "manual_chroma_median":M16_MANUAL_REFERENCE['metrics']['chroma_median'],
        "manual_color_contrast_index":0.3920477227,
        "strong_stop_neighborhood":"A reviewed candidate around median 0.17-0.27, p99.9 <=0.72, saturation >=0.26 and color_contrast_index >=0.30 is a strong reason to stop rather than chase more brightness."
      }
    return out

def review_plan(state:dict[str,Any],round_result:dict[str,Any])->dict[str,Any]:
    active_version(state)
    cands,rejected=reviewable_round_candidates(round_result)
    compared=[c['candidate'] for c in cands]
    recommended=round_result.get('recommended_candidate')
    if recommended not in compared:
        raise OrchestrationError(f'Recommended candidate is not reviewable/eligible: {recommended}')
    return {"status":"visual_review_required","review_scope":"round","orchestration_version":VERSION,
            "project_name":state['project_name'],"run_root":state['run_root'],"round":round_result['round'],
            "attempted_candidate_order":[c['candidate'] for c in round_result['candidates']],
            "recommended_candidate":recommended,"publication_eligible_candidates":round_result['eligible_candidates'],
            "compared_candidate_order":compared,"technically_rejected_candidates":rejected,
            "candidate_metrics":[compact_metrics(c) for c in cands],
            "continuation_advisory":continuation_advisory(state,round_result,cands),
            "manual_calibration_reference":calibration_reference(state),
            "read_targets":[{"candidate":c['candidate'],"path":c['preview']['path'],"sha256":c['preview']['sha256']} for c in cands],
            "required_review_fields":["brightness","contrast","color","structure","background","highlights"],
            "read_target_policy":{"path_handling":"verbatim","directory_discovery_forbidden":True,
               "forbidden_recovery_tools":["ls","find","grep","jq","globbing"],"on_read_failure":"stop_and_report_exact_failed_path"},
            "select_round_cli_contract":{"compared":"Repeat --compared once per candidate in compared_candidate_order.",
               "note":"Repeat --note once per compared candidate as candidate-ID=brightness:...; contrast:...; color:...; structure:...; background:...; highlights:...",
               "example_compared_flags":[f'--compared {cid}' for cid in compared]},
            "selection_rule":"Choose the technically eligible candidate with the best balance of brightness, contrast, genuinely separated color, structure, acceptable background, and safe highlights. Give color_contrast_index equal standing with absolute saturation/chroma. Penalize pastel washout where luminance rises faster than chroma. Histogram locations and any M16 manual reference are advisory only.",
            "minimum_rounds":MIN_ROUNDS,"maximum_rounds":MAX_ROUNDS,
            "instruction":"Read every returned target verbatim and compare every reviewable candidate. Reconcile visual appearance with saturation, chroma, color_contrast_index, median and p99.9. A lifted background is acceptable, but washed/pastel color from excess luminance is not. Round 1 must continue. From round 2 onward, stop when another cycle would mainly raise luminance/upper-tail placement without a material gain in color separation or structure; do not chase brightness for its own sake."}

def final_review_plan(state:dict[str,Any])->dict[str,Any]:
    panel=state.get('final_candidates')
    if not isinstance(panel,list) or len(panel)!=3:
        raise OrchestrationError('Final review state must contain exactly three candidates.')
    expected=[]
    for x in panel:
        cid=x.get('candidate'); preview=x.get('preview'); output=x.get('output')
        if not cid or not isinstance(preview,dict) or not preview.get('path') or not preview.get('sha256'):
            raise OrchestrationError(f'Final review candidate has invalid preview metadata: {cid!r}')
        if not isinstance(output,dict) or not output.get('path') or not output.get('sha256'):
            raise OrchestrationError(f'Final review candidate has invalid output metadata: {cid!r}')
        expected.append(cid)
    return {"status":"visual_review_required","review_scope":"final","orchestration_version":VERSION,"project_name":state['project_name'],
            "run_root":state['run_root'],"read_targets":[{"candidate":x['candidate'],"path":x['preview']['path'],"sha256":x['preview']['sha256']} for x in panel],
            "candidate_metrics":[compact_metrics(x) for x in panel],"manual_calibration_reference":calibration_reference(state),
            "required_review_fields":["brightness","contrast","color","structure","background","highlights","overall_balance"],
            "read_target_policy":{"path_handling":"verbatim","directory_discovery_forbidden":True,
              "forbidden_recovery_tools":["ls","find","grep","jq","globbing"],"on_read_failure":"stop_and_report_exact_failed_path"},
            "select_publish_cli_contract":{
              "candidate":"Use --candidate FINAL-ID. --selected is accepted as an alias.",
              "compared":"Do not pass --compared; the runtime derives and records the exact final candidate order automatically.",
              "note":"Repeat --note once per final candidate as FINAL-ID=brightness:...; contrast:...; color:...; structure:...; background:...; highlights:...; overall_balance:...",
              "minimum_note_characters":80,
              "example":"select-publish --project <project> --run-root <run-root> --candidate final-candidate-00 --note 'final-candidate-00=...' --note 'final-candidate-01=...' --note 'final-candidate-02=...'"},
            "completion_gate":"Do not report this stage complete or ready for green reduction unless select-publish returns status=ready. If publication returns blocked, correct the arguments/notes and retry; do not summarize success. After any failed publication attempt, stage-status may be used to verify the active run is still awaiting final review.",
            "selection_rule":"Select the final candidate with the best overall balance of brightness, contrast, genuinely separated color, preserved structure, acceptable background and zero newly clipped data. Ground color claims in saturation, chroma and color_contrast_index. Prefer a slightly less-stretched candidate when additional luminance makes the palette look pastel or washed. A later round does not automatically win.",
            "instruction":"Read all three final targets verbatim, compare them, then call select-publish. Omit --compared; repeat --note once per final candidate with all required fields including overall_balance. Never report completion until select-publish itself returns status=ready."}

def final_panel(state:dict[str,Any])->dict[str,Any]:
    winners=state['round_winners']; choices=[]
    def add_unique(x):
        if x is not None and not any((q['round'],q['candidate'])==(x['round'],x['candidate']) for q in choices): choices.append(x)
    def color_key(x):
        m=x.get('metrics') or {}; return float(m.get('saturation_median') or 0.0)+1.5*float(m.get('chroma_median') or 0.0)
    def brightness_key(x):
        m=x.get('metrics') or {}; return 0.55*float(m.get('p50') or 0.0)+0.45*float(m.get('p95') or 0.0)
    # Build a deliberately diverse final panel: overall technical balance, richest
    # colour checkpoint, and strongest useful brightness checkpoint. A later round
    # is not automatically better and a darker background is not automatically better.
    if winners:
        add_unique(max(winners,key=lambda x:x['technical_score']))
        add_unique(max(winners,key=color_key))
        add_unique(max(winners,key=brightness_key))
    for x in sorted(winners,key=lambda x:x['technical_score'],reverse=True):
        if len(choices)>=3: break
        add_unique(x)
    # With fewer than three distinct round winners, add the best non-winning candidate from the final round.
    if len(choices)<3:
        final_round=load_json(Path(state['run_root'])/f"round-{state['current_round']:02d}"/'round-result.json')
        existing={(x['round'],x['candidate']) for x in choices}
        extras=[]
        for c in final_round['candidates']:
            if c['eligible'] and (c['round'],c['candidate']) not in existing:
                extras.append({"round":c['round'],"candidate":c['candidate'],"technical_score":c['technical_score'],
                               "metrics":c.get('metrics'),"ghs":c.get('ghs'),"bp":c.get('bp'),"bp_target_floor":c.get('bp_target_floor'),
                               "round_relative_color_score":c.get('round_relative_color_score'),
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
                      "metrics":x.get('metrics'),"ghs":x.get('ghs'),"bp":x.get('bp'),"bp_target_floor":x.get('bp_target_floor'),
                      "round_relative_color_score":x.get('round_relative_color_score'),
                      "output":{"path":str(fit),"sha256":sha(fit)},"preview":{"path":str(png),"sha256":sha(png)}})
    state['final_candidates']=panel; state['status']='awaiting_final_review'; save_active(paths(state['project_name']),state)
    return final_review_plan(state)

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
        active_version(active)
        if active.get('source_sha256')!=sha(p['source']): raise OrchestrationError('Active stretch run belongs to an older channel-balance source; preserve it and start only after explicit recovery.')
        if active['status']=='awaiting_round_review':
            rr=load_json(Path(active['run_root'])/f"round-{active['current_round']:02d}"/'round-result.json'); return emit(review_plan(active,rr))
        if active['status']=='awaiting_final_review': return emit(final_review_plan(active))
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

def notes(values:list[str],expected:list[str],minimum_length:int=120,require_overall:bool=False)->dict[str,str]:
    out={}
    for raw in values:
        if '=' not in raw: raise OrchestrationError('Each --note must be candidate=structured observation.')
        k,v=raw.split('=',1); k=k.strip(); v=' '.join(v.split())
        if k not in expected: raise OrchestrationError(f'Unexpected candidate note: {k}')
        if k in out: raise OrchestrationError(f'Duplicate candidate note: {k}')
        required_labels=['brightness:','contrast:','color:','structure:','background:','highlights:']
        if require_overall: required_labels.append('overall_balance:')
        lower=v.lower()
        missing=[label for label in required_labels if label not in lower]
        if missing: raise OrchestrationError(f'Visual note for {k} is missing required fields: {missing}')
        if len(v)<minimum_length: raise OrchestrationError(f'Visual note for {k} is too vague; give specific observations for every required field (minimum {minimum_length} characters).')
        out[k]=v
    if set(out)!=set(expected): raise OrchestrationError(f'Notes must cover every compared candidate exactly: {expected}')
    return out

def command_select_round(a)->int:
    p,_=validate_project(a.project); state=load_active(p)
    if not state: raise OrchestrationError('No active stretch run.')
    prior_version=active_version(state)
    if Path(a.run_root).resolve()!=Path(state['run_root']).resolve(): raise OrchestrationError('Run root does not match active run.')
    if state['status']!='awaiting_round_review': raise OrchestrationError('Active run is not awaiting a round review.')
    rr=load_json(Path(state['run_root'])/f"round-{state['current_round']:02d}"/'round-result.json')
    reviewable,_=reviewable_round_candidates(rr)
    compared=a.compared; expected=[c['candidate'] for c in reviewable]
    if compared!=expected: raise OrchestrationError(f'--compared must repeat exact reviewable candidate order using repeated flags: {expected}')
    if a.candidate not in expected: raise OrchestrationError('Selected round candidate is not technically eligible/reviewable.')
    n=notes(a.note,expected,minimum_length=120,require_overall=False); selected=next(c for c in reviewable if c['candidate']==a.candidate)
    record={"round":state['current_round'],"selected_candidate":a.candidate,"compared":compared,"notes":n,"selected_output":selected['output'],
            "selected_preview":selected['preview'],"technical_score":selected['technical_score'],"continue_requested":a.continue_round=='yes',
            "orchestration_version":VERSION,"resumed_from_orchestration_version":prior_version if prior_version!=VERSION else None}
    eng.atomic_json(Path(state['run_root'])/f"round-{state['current_round']:02d}"/'visual-selection-record.json',record)
    state['round_winners'].append({"round":state['current_round'],"candidate":a.candidate,"technical_score":selected['technical_score'],
                                   "metrics":selected.get('metrics'),"ghs":selected.get('ghs'),"bp":selected.get('bp'),
                                   "bp_target_floor":selected.get('bp_target_floor'),"round_relative_color_score":selected.get('round_relative_color_score'),
                                   "output":selected['output'],"preview":selected['preview']})
    if prior_version!=VERSION: state['resumed_from_orchestration_version']=prior_version
    state['orchestration_version']=VERSION
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
    prior_version=active_version(state)
    if prior_version!=VERSION:
        state['resumed_from_orchestration_version']=prior_version; state['orchestration_version']=VERSION
    if Path(a.run_root).resolve()!=Path(state['run_root']).resolve(): raise OrchestrationError('Run root mismatch.')
    if state['status']!='awaiting_final_review': raise OrchestrationError('Active run is not awaiting final review.')
    expected=[x['candidate'] for x in state['final_candidates']]
    supplied_compared=list(a.compared or [])
    if supplied_compared:
        if len(supplied_compared)!=len(expected) or set(supplied_compared)!=set(expected):
            raise OrchestrationError(f'If --compared is supplied, it must contain each final candidate exactly once: {expected}')
    compared=expected
    if a.candidate not in expected: raise OrchestrationError('Selected final candidate is not in the exact final panel.')
    n=notes(a.note,expected,minimum_length=80,require_overall=True); sel=next(x for x in state['final_candidates'] if x['candidate']==a.candidate)
    review={"schema_version":1,"project":a.project,"run_root":state['run_root'],"visual_review_completed":True,"selected_candidate":a.candidate,
            "selected_source_round":sel['source_round'],"selected_source_candidate":sel['source_candidate'],"compared":compared,"notes":n,"reviewed_at":time.time()}
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
    x=s.add_parser('select-publish'); x.add_argument('--project',required=True); x.add_argument('--run-root',required=True); x.add_argument('--candidate','--selected',dest='candidate',required=True); x.add_argument('--compared',action='append'); x.add_argument('--note',action='append',required=True); x.set_defaults(func=command_select_publish)
    x=s.add_parser('stage-status'); x.add_argument('--project',required=True); x.set_defaults(func=command_status)
    x=s.add_parser('self-test'); x.add_argument('--root'); x.add_argument('--timeout',type=int,default=600); x.add_argument('--keep',action='store_true'); x.set_defaults(func=command_self_test)
    return p

def main()->int:
    a=parser().parse_args()
    try: return int(a.func(a))
    except (OrchestrationError,eng.StretchError) as e:
        return emit({"status":"blocked","orchestration_version":VERSION,"error":str(e),"action":"stop_no_discovery"},2)
    except Exception as e:
        return emit({"status":"blocked","orchestration_version":VERSION,
                     "error":f"Unexpected internal error: {type(e).__name__}: {e}",
                     "action":"stop_no_discovery","internal_error":True},3)

if __name__=='__main__': raise SystemExit(main())
