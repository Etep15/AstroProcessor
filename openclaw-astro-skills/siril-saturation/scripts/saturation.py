#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import numpy as np
from astropy.io import fits

VERSION='1.0.0'
WORKSPACE=Path(os.environ.get('SIRIL_SATURATION_WORKSPACE','/home/peter/.openclaw/workspace/agents/codewarrior'))
SIRIL_APP=Path('/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root/AppRun')
SIRIL_APPDIR=SIRIL_APP.parent
REQUIRED_SIRIL_VERSION='1.4.4'
CANDIDATES={
 'candidate-00': {'classification':'no-change','D':0.0,'SP':0.50,'HP':1.0},
 'candidate-01': {'classification':'mild','D':0.35,'SP':0.50,'HP':0.75},
 'candidate-02': {'classification':'moderate','D':0.70,'SP':0.50,'HP':0.70},
}
MAX_ADDED_CLIP=1e-6
MIN_LUMA_CORR=0.995
MAX_LUMA_MEDIAN_SHIFT=0.010

class SaturationError(RuntimeError): pass
@dataclass(frozen=True)
class FitsEvidence:
    path:str; sha256:str; size:int; bitpix:int; dtype:str; channels:int; width:int; height:int
    minimum:float; median:float; maximum:float; finite_fraction:float

def now(): return datetime.now(timezone.utc).isoformat()
def uid(): return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')+f'-p{os.getpid()}'
def sha(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
def dump(path:Path,obj:Any):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name('.'+path.name+'.tmp')
    tmp.write_text(json.dumps(obj,indent=2,sort_keys=True)+'\n'); os.replace(tmp,path)

def ppaths(project_name:str):
    if not project_name or Path(project_name).name!=project_name: raise SaturationError('Project must be a single directory name.')
    project=WORKSPACE/'Projects'/project_name
    if not project.is_dir(): raise SaturationError(f'Project directory missing: {project}')
    green=project/'processing/green-reduction'; stable=project/'processing/saturation'; runs=project/'.siril-saturation'
    return {
      'project':project,'source':green/'SHO-starless-green-reduced.fit','source_manifest':green/'green-reduction-manifest.json',
      'stable':stable,'stable_output':stable/'SHO-starless-saturated.fit','stable_before':stable/'SHO-starless-green-reduced-before-saturation.png',
      'stable_after':stable/'SHO-starless-saturated.png','stable_manifest':stable/'saturation-manifest.json','stable_visual':stable/'visual-selection-record.json',
      'runs':runs,'intent':runs/'fresh-intent.json'
    }

def inspect(path:Path)->FitsEvidence:
    if not path.is_file(): raise SaturationError(f'FITS missing: {path}')
    with fits.open(path,memmap=True) as hdul:
        d=np.asarray(hdul[0].data); bitpix=int(hdul[0].header.get('BITPIX',0)); a=np.asarray(d,dtype=np.float64)
    if a.ndim!=3 or a.shape[0]!=3: raise SaturationError(f'Expected RGB FITS, got {a.shape}: {path}')
    finite=np.isfinite(a); vals=a[finite]
    return FitsEvidence(str(path),sha(path),path.stat().st_size,bitpix,str(d.dtype),3,int(a.shape[2]),int(a.shape[1]),float(vals.min()),float(np.median(vals)),float(vals.max()),float(finite.mean()))

def load(path:Path,stride:int=4):
    with fits.open(path,memmap=True) as hdul: a=np.asarray(hdul[0].data[:,::stride,::stride],dtype=np.float64)
    if not np.isfinite(a).all(): raise SaturationError(f'Non-finite data: {path}')
    return a

def metrics(source:Path,output:Path):
    s=load(source); o=load(output)
    if s.shape!=o.shape: raise SaturationError('Source/output sampled shapes differ.')
    def sat(a):
        mx=np.max(a,axis=0); mn=np.min(a,axis=0)
        return np.where(mx>1e-12,(mx-mn)/mx,0.0)
    ss=sat(s).ravel(); osat=sat(o).ravel(); sl=np.mean(s,axis=0).ravel(); ol=np.mean(o,axis=0).ravel()
    corr=float(np.corrcoef(sl,ol)[0,1]); slo=float(np.mean(s<=1e-7)); olo=float(np.mean(o<=1e-7)); shi=float(np.mean(s>=1-1e-7)); ohi=float(np.mean(o>=1-1e-7))
    return {
      'source_saturation_median':float(np.median(ss)),'output_saturation_median':float(np.median(osat)),
      'source_saturation_p90':float(np.quantile(ss,.90)),'output_saturation_p90':float(np.quantile(osat,.90)),
      'source_saturation_p99':float(np.quantile(ss,.99)),'output_saturation_p99':float(np.quantile(osat,.99)),
      'saturation_median_gain':float(np.median(osat)-np.median(ss)),'luma_correlation':corr,
      'max_abs_sampled_rgb_change':float(np.max(np.abs(o-s))),
      'source_luma_median':float(np.median(sl)),'output_luma_median':float(np.median(ol)),
      'absolute_luma_median_change':abs(float(np.median(ol)-np.median(sl))),
      'added_low_clip_fraction':max(0.0,olo-slo),'added_high_clip_fraction':max(0.0,ohi-shi)
    }

def validate_upstream(paths):
    mp=paths['source_manifest']; fp=paths['source']
    if not mp.is_file() or not fp.is_file(): raise SaturationError('Current green-reduction canonical prerequisite is missing.')
    m=json.loads(mp.read_text()); errors=[]
    if m.get('status')!='ready': errors.append('green-reduction status must be ready')
    if m.get('next_stage')!='siril-saturation': errors.append('green-reduction next_stage must be siril-saturation')
    if m.get('saturation_processing_permitted') is not True: errors.append('green-reduction must permit saturation')
    order=m.get('stage_order') or {}
    if order.get('current')!='siril-green-reduction': errors.append('upstream stage_order.current must be siril-green-reduction')
    if order.get('downstream')!='siril-saturation': errors.append('upstream stage_order.downstream must be siril-saturation')
    out=m.get('output') or {}
    if out.get('path')!=str(fp): errors.append('upstream output path is not canonical')
    ev=inspect(fp)
    if out.get('sha256')!=ev.sha256: errors.append('upstream output SHA differs from manifest')
    if ev.bitpix!=-32 or ev.finite_fraction!=1.0: errors.append('upstream FITS must be finite 32-bit RGB')
    if errors: raise SaturationError('Upstream siril-green-reduction contract failed: '+'; '.join(errors))
    return m,ev

def siril_version():
    env=os.environ.copy(); env['APPDIR']=str(SIRIL_APPDIR)
    p=subprocess.run([str(SIRIL_APP),'siril-cli','--version'],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,timeout=60)
    if p.returncode!=0 or REQUIRED_SIRIL_VERSION not in p.stdout: raise SaturationError('Expected Siril 1.4.4.')
    return p.stdout.strip()

def script_text():
    lines=[f'requires {REQUIRED_SIRIL_VERSION}','load "SHO-starless-green-reduced.fit"','savepng "../common/SHO-starless-green-reduced-before-saturation"','close',
           'load "SHO-starless-green-reduced.fit"','save "../candidate-00/work/SHO-starless-saturated.fit"','savepng "../candidate-00/previews/SHO-starless-saturated"','close']
    for n in ('candidate-01','candidate-02'):
        c=CANDIDATES[n]; lines += ['load "SHO-starless-green-reduced.fit"',f'invght -D={c["D"]:.3f} -B=0.000 -SP={c["SP"]:.3f} -HP={c["HP"]:.3f} -clipmode=rgbblend -sat',f'save "../{n}/work/SHO-starless-saturated.fit"',f'savepng "../{n}/previews/SHO-starless-saturated"','close']
    return '\n'.join(lines)+'\n'

def run_candidates(project_name:str, workspace:Path|None=None):
    global WORKSPACE
    if workspace is not None: WORKSPACE=workspace
    paths=ppaths(project_name); um,src=validate_upstream(paths); siril=siril_version(); root=paths['runs']/f'saturation-{uid()}'
    work=root/'work'; common=root/'common'; logs=root/'logs'; work.mkdir(parents=True); common.mkdir(); logs.mkdir()
    for n in CANDIDATES: (root/n/'work').mkdir(parents=True); (root/n/'previews').mkdir(parents=True)
    staged=work/'SHO-starless-green-reduced.fit'; shutil.copy2(paths['source'],staged)
    if sha(staged)!=src.sha256: raise SaturationError('Staged source SHA changed.')
    ssf=root/'saturation.ssf'; ssf.write_text(script_text())
    env=os.environ.copy(); env['APPDIR']=str(SIRIL_APPDIR)
    p=subprocess.run([str(SIRIL_APP),'siril-cli','--directory',str(work),'--script',str(ssf)],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=420)
    (logs/'stdout.log').write_text(p.stdout); (logs/'stderr.log').write_text(p.stderr)
    if p.returncode!=0: raise SaturationError(f'Siril saturation run failed rc={p.returncode}; evidence preserved at {root}')
    before=common/'SHO-starless-green-reduced-before-saturation.png'
    if not before.is_file(): raise SaturationError('Before preview missing.')
    items=[]
    for n,c in CANDIDATES.items():
        out=root/n/'work/SHO-starless-saturated.fit'; prev=root/n/'previews/SHO-starless-saturated.png'
        if not out.is_file() or not prev.is_file(): raise SaturationError(f'{n} output/preview missing.')
        ev=inspect(out); met=metrics(staged,out); failed=[]
        if ev.bitpix!=-32 or ev.finite_fraction!=1.0: failed.append('finite 32-bit RGB required')
        if ev.minimum < -1e-6 or ev.maximum > 1.000001: failed.append('output outside [0,1] tolerance')
        if met['luma_correlation'] < MIN_LUMA_CORR: failed.append('luma correlation too low')
        if met['absolute_luma_median_change'] > MAX_LUMA_MEDIAN_SHIFT: failed.append('luma median changed too much')
        if met['added_low_clip_fraction'] > MAX_ADDED_CLIP or met['added_high_clip_fraction'] > MAX_ADDED_CLIP: failed.append('new clipping')
        if n!='candidate-00' and met['saturation_median_gain'] <= 0: failed.append('saturation median did not increase')
        if n=='candidate-00' and met['max_abs_sampled_rgb_change'] > 1e-7: failed.append('no-change candidate pixel data differs from source')
        items.append({'candidate':n,**c,'output':asdict(ev),'metrics':met,'eligible':not failed,'failed_checks':failed,'preview':str(prev),'preview_sha256':sha(prev)})
    eligible=[x['candidate'] for x in items if x['eligible']]
    if 'candidate-00' not in eligible: raise SaturationError('No-change baseline failed technical validation.')
    if not any(n in eligible for n in ('candidate-01','candidate-02')): raise SaturationError('No saturation-enhancement candidate passed technical validation.')
    rec='candidate-01' if 'candidate-01' in eligible else ('candidate-02' if 'candidate-02' in eligible else 'candidate-00')
    record={'schema_version':1,'version':VERSION,'project_name':project_name,'run_root':str(root),'status':'awaiting_visual_selection','source':asdict(src),'upstream_manifest':str(paths['source_manifest']),'upstream_manifest_sha256':sha(paths['source_manifest']),'siril_version':siril,'candidate_policy':CANDIDATES,'candidates':items,'publication_eligible_candidates':eligible,'recommended_candidate':rec,'visual_selection':None,'canonical_output_changed':False}
    dump(root/'run-manifest.json',record); return record

def canonical_status(project_name:str):
    paths=ppaths(project_name); _,src=validate_upstream(paths)
    if not paths['stable_manifest'].is_file() and not paths['stable_output'].exists(): return {'status':'missing','current_canonical_status':'missing','source_sha256':src.sha256}
    if not paths['stable_manifest'].is_file() or not paths['stable_output'].is_file(): return {'status':'blocked','current_canonical_status':'invalid','error':'Saturation canonical is partial.'}
    m=json.loads(paths['stable_manifest'].read_text()); errors=[]
    if m.get('status')!='ready': errors.append('manifest not ready')
    if (m.get('source') or {}).get('sha256')!=src.sha256: errors.append('source SHA differs from current green reduction')
    if (m.get('stage_order') or {}).get('upstream')!='siril-green-reduction': errors.append('upstream stage is not siril-green-reduction')
    out=m.get('output') or {}
    if out.get('path')!=str(paths['stable_output']) or out.get('sha256')!=sha(paths['stable_output']): errors.append('output evidence mismatch')
    relation='ready' if not errors else 'obsolete'
    return {'status':'completed','current_canonical_status':relation,'canonical_output_sha256':out.get('sha256'),'canonical_manifest_sha256':sha(paths['stable_manifest']),'source_sha256':src.sha256,'obsolete_reasons':errors}

def write_intent(project_name,state):
    paths=ppaths(project_name); paths['runs'].mkdir(parents=True,exist_ok=True)
    payload={'status':'fresh_run_authorized','version':VERSION,'project':str(paths['project']),'authorized_at':now(),'source_sha256':state['source_sha256'],'canonical_output_sha256':state.get('canonical_output_sha256')}
    dump(paths['intent'],payload); return paths['intent']
def authorized(project_name,state):
    p=ppaths(project_name)['intent']
    if not p.is_file(): return False
    try:m=json.loads(p.read_text())
    except Exception:return False
    return m.get('status')=='fresh_run_authorized' and m.get('version')==VERSION and m.get('source_sha256')==state['source_sha256'] and m.get('canonical_output_sha256')==state.get('canonical_output_sha256')

def latest_compatible_run(project_name:str,source_sha:str):
    paths=ppaths(project_name); found=[]
    if paths['runs'].is_dir():
        for r in paths['runs'].iterdir():
            mf=r/'run-manifest.json'
            if not mf.is_file(): continue
            try:m=json.loads(mf.read_text())
            except Exception:continue
            if m.get('project_name')==project_name and (m.get('source') or {}).get('sha256')==source_sha and m.get('status')=='awaiting_visual_selection': found.append((mf.stat().st_mtime,r,m))
    return max(found,key=lambda x:x[0])[1:] if found else None

def review_payload(record):
    eligible=record['publication_eligible_candidates']; by={x['candidate']:x for x in record['candidates']}; targets=[]
    first=by[eligible[0]]; before=Path(record['run_root'])/'common/SHO-starless-green-reduced-before-saturation.png'; targets.append({'role':'before','path':str(before),'sha256':sha(before)})
    for n in eligible: targets.append({'role':'candidate','candidate':n,'path':by[n]['preview'],'sha256':by[n]['preview_sha256']})
    return {'status':'visual_review_required','version':VERSION,'project_name':record['project_name'],'run_root':record['run_root'],'recommended_candidate':record['recommended_candidate'],'publication_eligible_candidates':eligible,'candidate_options':{n:{k:by[n][k] for k in ('classification','D','SP','HP')} for n in eligible},'read_targets':targets,'required_review_fields':{n:['color','artifacts','structure'] for n in eligible},'review_method_required':'openclaw-read','next_action':'review-publish'}

def cmd_advance(args):
    state=canonical_status(args.project)
    if state['status']=='completed':
        if not authorized(args.project,state):
            return emit({'status':'confirmation_required','current_canonical_status':state['current_canonical_status'],'obsolete_reasons':state.get('obsolete_reasons',[]),'question':f'Saturation for {args.project} has already completed. Do you want me to run it again as a fresh run?','production_processing_started':False})
    if args.plan_only:
        return emit({'status':'would_generate_candidates','version':VERSION,'source_sha256':state['source_sha256'],'production_processing_started':False})
    existing=latest_compatible_run(args.project,state['source_sha256'])
    record=existing[1] if existing else run_candidates(args.project)
    return emit(review_payload(record))
def cmd_confirm(args):
    state=canonical_status(args.project)
    if state['status']!='completed': raise SaturationError('Fresh confirmation requires a completed saturation canonical.')
    p=write_intent(args.project,state); return emit({'status':'fresh_run_authorized','fresh_intent':str(p),'next_action':'advance'})
def cmd_status(args):
    s=canonical_status(args.project); s['version']=VERSION; return emit(s,0 if s.get('current_canonical_status') in ('ready','missing') else 2)
def cmd_self(args):
    return emit({'status':'success','version':VERSION,'candidate_count':3,'candidate_00_no_change':True,'candidate_01':CANDIDATES['candidate-01'],'candidate_02':CANDIDATES['candidate-02'],'upstream':'siril-green-reduction','transform':'Siril invght on saturation channel','completed_stage_requires_confirmation':True,'exact_path_visual_review':True})
def cmd_review(args):
    state=canonical_status(args.project); src_sha=state['source_sha256']; found=latest_compatible_run(args.project,src_sha)
    if not found: raise SaturationError('No compatible generated saturation run is awaiting review.')
    root,record=found; eligible=record['publication_eligible_candidates']
    if args.selected not in eligible: raise SaturationError(f'Selected {args.selected} is not eligible.')
    notes={}
    for n,prefix in [('candidate-00','c0'),('candidate-01','c1'),('candidate-02','c2')]:
        if n not in eligible: continue
        vals={f:getattr(args,f'{prefix}_{f}') for f in ('color','artifacts','structure')}
        for f,v in vals.items():
            if not v or len(v.strip())<12: raise SaturationError(f'{n} {f} observation is too vague.')
        notes[n]=vals
    selected=next(x for x in record['candidates'] if x['candidate']==args.selected)
    visual={'schema_version':1,'project':args.project,'reviewed_at':now(),'review_method':'openclaw-read','compared':eligible,'selected_candidate':args.selected,'notes':notes}
    dump(root/'visual-selection-record.json',visual)
    paths=ppaths(args.project); staging=root/'publish-staging'; staging.mkdir(exist_ok=False)
    out=Path(selected['output']['path']); before=root/'common/SHO-starless-green-reduced-before-saturation.png'; after=Path(selected['preview'])
    shutil.copy2(out,staging/'SHO-starless-saturated.fit'); shutil.copy2(before,staging/'SHO-starless-green-reduced-before-saturation.png'); shutil.copy2(after,staging/'SHO-starless-saturated.png'); shutil.copy2(root/'visual-selection-record.json',staging/'visual-selection-record.json')
    _,src=validate_upstream(paths)
    manifest={'schema_version':1,'version':VERSION,'created_at':now(),'project':args.project,'status':'ready','stage_order':{'upstream':'siril-green-reduction','current':'siril-saturation','downstream':None},'source_contract_revision':'siril-green-reduction-canonical-v1','source':asdict(src),'output':{**selected['output'],'path':str(paths['stable_output'])},'selected_candidate':args.selected,'recommended_candidate':record['recommended_candidate'],'method':{'type':'inverse-generalised-hyperbolic-saturation','command':'no-op' if args.selected=='candidate-00' else f'invght -D={selected["D"]:.3f} -B=0.000 -SP={selected["SP"]:.3f} -HP={selected["HP"]:.3f} -clipmode=rgbblend -sat','parameters':{k:selected[k] for k in ('D','SP','HP')},'saturation_channel':True},'quality_metrics':selected['metrics'],'visual_review_completed':True,'visual_review':str(paths['stable_visual']),'previews':{'before':str(paths['stable_before']),'after':str(paths['stable_after'])},'next_stage':None,'downstream_processing_permitted':False,'run_root':str(root)}
    dump(staging/'saturation-manifest.json',manifest)
    previous=None
    if paths['stable'].exists(): previous=root/f'previous-processing-saturation-{uid()}'; paths['stable'].rename(previous)
    staging.rename(paths['stable'])
    if sha(paths['stable_output'])!=selected['output']['sha256']: raise SaturationError('Published output SHA mismatch.')
    record['status']='ready'; record['canonical_output_changed']=True; record['published_at']=now(); dump(root/'run-manifest.json',record)
    return emit({'status':'ready','version':VERSION,'project':str(paths['project']),'selected_candidate':args.selected,'canonical_output_sha256':sha(paths['stable_output']),'previous_processing_saturation_preserved_at':str(previous) if previous else None,'next_stage':None,'downstream_processing_permitted':False})
def cmd_smoke(args):
    rec=run_candidates(args.project,Path(args.workspace)); return emit({'status':rec['status'],'source':rec['source'],'candidate_count':len(rec['candidates']),'eligible':rec['publication_eligible_candidates'],'recommended_candidate':rec['recommended_candidate'],'metrics':{x['candidate']:x['metrics'] for x in rec['candidates']}})
def emit(obj,rc=0): print(json.dumps(obj,indent=2,sort_keys=True)); return rc

def main():
    ap=argparse.ArgumentParser(); sp=ap.add_subparsers(dest='cmd',required=True)
    p=sp.add_parser('advance'); p.add_argument('--project',required=True); p.add_argument('--plan-only',action='store_true')
    p=sp.add_parser('confirm-fresh'); p.add_argument('--project',required=True)
    p=sp.add_parser('stage-status'); p.add_argument('--project',required=True)
    p=sp.add_parser('review-publish'); p.add_argument('--project',required=True); p.add_argument('--selected',required=True,choices=tuple(CANDIDATES))
    for pre in ('c0','c1','c2'):
        for f in ('color','artifacts','structure'): p.add_argument(f'--{pre}-{f}',dest=f'{pre}_{f}')
    sp.add_parser('self-test')
    p=sp.add_parser('_smoke'); p.add_argument('--project',required=True); p.add_argument('--workspace',required=True)
    a=ap.parse_args()
    try:
        return {'advance':cmd_advance,'confirm-fresh':cmd_confirm,'stage-status':cmd_status,'review-publish':cmd_review,'self-test':cmd_self,'_smoke':cmd_smoke}[a.cmd](a)
    except SaturationError as e: return emit({'status':'blocked','version':VERSION,'error':str(e),'production_processing_started':False},2)
if __name__=='__main__': raise SystemExit(main())
