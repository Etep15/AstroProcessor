#!/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python
from __future__ import annotations
import hashlib,json,os,shutil,subprocess,time
from pathlib import Path
from typing import Any
import numpy as np
from astropy.io import fits
from ghs_pass import GHSParameters,bounded
from black_point_pass import bp_for_floor,backoff_factors,command as bp_command

VERSION="1.2.4"
SIRIL=Path("/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root/AppRun")

# Calibrated at install time by the non-destructive M16 stride-4 search.
# Values are relative policy profiles, not M16 histogram targets.  The installer
# replaces this exact block only after a zero-clipping profile search succeeds.
ROUND2_PROFILES = [{'B': 0.0, 'bp_min_fraction': 0.985, 'color_mode': 'independent', 'd_scale': 1.0, 'lp_fraction': 0.0, 'name': 'conservative', 'sp_scale': 1.27}, {'B': 0.0, 'bp_min_fraction': 0.985, 'color_mode': 'independent', 'd_scale': 0.88, 'lp_fraction': 0.0, 'name': 'balanced', 'sp_scale': 1.12}, {'B': 0.0, 'bp_min_fraction': 0.985, 'color_mode': 'independent', 'd_scale': 1.0, 'lp_fraction': 0.35, 'name': 'color-contrast', 'sp_scale': 1.27}]
# END_ROUND2_PROFILES

class StretchError(RuntimeError): pass

def sha256_file(path: Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for c in iter(lambda:f.read(1024*1024),b''): h.update(c)
    return h.hexdigest()

def atomic_json(path:Path,payload:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.'+path.name+'.tmp')
    tmp.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    os.replace(tmp,path)

def rgb_array(path:Path,stride:int=1)->np.ndarray:
    with fits.open(path,memmap=True) as h:
        a=np.asarray(h[0].data,dtype=np.float32)
    if a.ndim==2: a=np.stack([a,a,a],axis=0)
    elif a.ndim==3 and a.shape[0] in (3,4): a=a[:3]
    elif a.ndim==3 and a.shape[-1] in (3,4): a=np.moveaxis(a[...,:3],-1,0)
    else: raise StretchError(f"Unsupported FITS shape {a.shape}: {path}")
    if stride>1: a=a[:,::stride,::stride]
    if not np.isfinite(a).all(): raise StretchError(f"Non-finite pixels in {path}")
    return a

def luma(a:np.ndarray)->np.ndarray:
    return np.mean(a[:3],axis=0,dtype=np.float64)

def robust_color_metrics(a:np.ndarray)->dict[str,float]:
    x=np.moveaxis(a[:3],0,-1).reshape(-1,3).astype(np.float64)
    mx=x.max(axis=1); mn=x.min(axis=1)
    mask=(mx>1e-6)&np.isfinite(mx)&np.isfinite(mn)
    if not np.any(mask):
        return {"saturation_median":0.0,"saturation_p75":0.0,"chroma_median":0.0,"chroma_p75":0.0}
    sat=(mx[mask]-mn[mask])/np.maximum(mx[mask],1e-9)
    chroma=(mx[mask]-mn[mask])
    return {
        "saturation_median":float(np.quantile(sat,0.50)),
        "saturation_p75":float(np.quantile(sat,0.75)),
        "chroma_median":float(np.quantile(chroma,0.50)),
        "chroma_p75":float(np.quantile(chroma,0.75)),
    }

def robust_saturation(a:np.ndarray)->float:
    return robust_color_metrics(a)["saturation_median"]

def histogram_mode(v:np.ndarray)->float:
    f=v[np.isfinite(v)]
    if f.size<100: return float(np.median(f))
    lo=max(0.0,float(np.quantile(f,0.001))); hi=min(1.0,float(np.quantile(f,0.995)))
    if hi<=lo: return float(np.median(f))
    hist,edges=np.histogram(f,bins=2048,range=(lo,hi))
    i=int(np.argmax(hist)); return float((edges[i]+edges[i+1])/2)

def metrics(path:Path,reference:Path|None=None,stride:int=2)->dict[str,Any]:
    # Hard clipping safety is evaluated against EVERY RGB pixel at full resolution.
    # Histogram/correlation calculations may remain stride-sampled for efficiency.
    full=rgb_array(path,stride=1)
    a=full[:,::stride,::stride] if stride>1 else full
    y=luma(a); f=y.reshape(-1)
    qlist=(0.01,0.05,0.1,1,5,10,25,50,75,90,95,99,99.5,99.9)
    qs={f"p{q:g}":float(np.quantile(f,q/100.0)) for q in qlist}
    channel_quantiles={}
    for q in (0.01,0.05,0.1,1,50,99,99.9):
        channel_quantiles[f"p{q:g}"]=[float(np.quantile(a[c],q/100.0)) for c in range(3)]
    color_metrics=robust_color_metrics(a)
    out={
        "path":str(path),"sha256":sha256_file(path),"minimum":float(full.min()),"maximum":float(full.max()),
        "finite_fraction":1.0,"mode":histogram_mode(f),"median":qs["p50"],**color_metrics,
        "low_clip_fraction":float(np.mean(full<=0.0)),"high_clip_fraction":float(np.mean(full>=1.0)),
        "channel_low_clip_fraction":[float(np.mean(full[c]<=0.0)) for c in range(3)],
        "channel_high_clip_fraction":[float(np.mean(full[c]>=1.0)) for c in range(3)],
        "channel_quantiles":channel_quantiles,
        "spread_p95_p05":qs["p95"]-qs["p5"],"spread_p99_p01":qs["p99"]-qs["p1"],**qs,
    }
    out["color_contrast_index"]=float(color_metrics["chroma_median"]/max(qs["p50"],1e-6))
    out["color_contrast_p75"]=float(color_metrics["chroma_p75"]/max(qs["p75"],1e-6))
    if reference is not None:
        bfull=rgb_array(reference,stride=1)
        if bfull.shape!=full.shape: raise StretchError("Reference shape mismatch")
        b=bfull[:,::stride,::stride] if stride>1 else bfull
        by=luma(b)
        if by.shape!=y.shape: raise StretchError("Reference shape mismatch")
        x=by.reshape(-1); z=f
        out["luma_correlation"]=float(np.corrcoef(x,z)[0,1]) if np.std(x)>0 and np.std(z)>0 else 1.0
        # Approximate Spearman/rank correlation on a deterministic bounded sample.
        if x.size>120000:
            idx=np.linspace(0,x.size-1,120000,dtype=np.int64); xr=x[idx]; zr=z[idx]
        else:
            xr=x; zr=z
        ox=np.argsort(xr,kind='mergesort'); oz=np.argsort(zr,kind='mergesort')
        rx=np.empty_like(ox,dtype=np.float64); rz=np.empty_like(oz,dtype=np.float64)
        rx[ox]=np.arange(ox.size,dtype=np.float64); rz[oz]=np.arange(oz.size,dtype=np.float64)
        out["luma_rank_correlation"]=float(np.corrcoef(rx,rz)[0,1]) if rx.size>1 else 1.0
        ref_color=robust_color_metrics(b)
        rs=ref_color["saturation_median"]
        rc=ref_color["chroma_median"]
        out["saturation_retention"]=(out["saturation_median"]/rs) if rs>1e-9 else 1.0
        out["chroma_retention"]=(out["chroma_median"]/rc) if rc>1e-9 else 1.0
        ref_cci=float(ref_color["chroma_median"]/max(float(np.quantile(by.reshape(-1),0.50)),1e-6))
        out["color_contrast_retention"]=(out["color_contrast_index"]/ref_cci) if ref_cci>1e-9 else 1.0
        out["low_clip_delta"]=out["low_clip_fraction"]-float(np.mean(bfull<=0.0))
        out["high_clip_delta"]=out["high_clip_fraction"]-float(np.mean(bfull>=1.0))
        out["channel_low_clip_delta"]=[out["channel_low_clip_fraction"][c]-float(np.mean(bfull[c]<=0.0)) for c in range(3)]
        out["channel_high_clip_delta"]=[out["channel_high_clip_fraction"][c]-float(np.mean(bfull[c]>=1.0)) for c in range(3)]
        # Exact policy metric: count newly clipped pixels, not merely a net change in counts.
        new_low=(bfull>0.0)&(full<=0.0)
        new_high=(bfull<1.0)&(full>=1.0)
        out["new_low_clip_fraction"]=float(np.mean(new_low))
        out["new_high_clip_fraction"]=float(np.mean(new_high))
        out["channel_new_low_clip_fraction"]=[float(np.mean(new_low[c])) for c in range(3)]
        out["channel_new_high_clip_fraction"]=[float(np.mean(new_high[c])) for c in range(3)]
    return out

def run_siril(workdir:Path,script_lines:list[str],timeout:int)->dict[str,Any]:
    workdir.mkdir(parents=True,exist_ok=True)
    script=workdir/'stage.ssf'; script.write_text('\n'.join(script_lines)+'\n',encoding='utf-8')
    env=os.environ.copy(); home=workdir/'.runner-home'; home.mkdir(exist_ok=True); env['HOME']=str(home)
    cmd=[str(SIRIL),'siril-cli','--offline','--directory',str(workdir),'--script',str(script)]
    started=time.time(); p=subprocess.run(cmd,text=True,capture_output=True,timeout=timeout,env=env)
    rec={"argv":cmd,"exit_code":p.returncode,"duration_seconds":time.time()-started,
         "stdout":p.stdout[-20000:],"stderr":p.stderr[-12000:],"script":str(script),
         "script_text":script.read_text(encoding='utf-8'),"script_sha256":sha256_file(script)}
    if p.returncode!=0:
        # Siril often reports script command errors on stdout while the AppImage
        # APPDIR warning occupies stderr. Preserve and print BOTH streams.
        failure=workdir/f"siril-failure-{int(time.time()*1000)}.json"
        atomic_json(failure,rec)
        raise StretchError(
            f"Siril failed ({p.returncode}) in {workdir}\n"
            f"SCRIPT:\n{rec['script_text']}\n"
            f"STDOUT (tail):\n{rec['stdout'][-6000:]}\n"
            f"STDERR (tail):\n{rec['stderr'][-4000:]}\n"
            f"Failure record: {failure}"
        )
    return rec

def _base_sp(source_metrics:dict[str,Any])->float:
    mode=float(source_metrics['mode']); median=float(source_metrics['p50'])
    sp=max(float(source_metrics['p1'])*1.01,min(mode,median*0.98))
    if sp<=1e-5: sp=max(1e-5,float(source_metrics['p10'])*0.85)
    return float(sp)


def _base_D(source_metrics:dict[str,Any],round_number:int)->float:
    p99=float(source_metrics['p99']); median=float(source_metrics['p50'])
    base_D={1:4.10,2:2.55,3:0.58,4:0.32,5:0.18}[round_number]
    if source_metrics['p95']<0.16: base_D*=1.08
    if p99>0.62: base_D*=0.82
    if p99>0.78: base_D*=0.78
    if median>0.34: base_D*=0.82
    return float(base_D)


def round2_parameters(source_metrics:dict[str,Any],profile:dict[str,Any])->GHSParameters:
    """Convert an install-calibrated relative Round-2 profile into Siril GHS parameters.

    v1.2.2 proved that a broad Independent GHS near D~2.4 generates the required
    channel separation.  v1.2.3 failed because it reduced D too far.  v1.2.4 keeps
    that energy and instead searches pivot/LP geometry plus the strongest BP that
    remains strictly below the actual post-GHS minimum.
    """
    base_D=_base_D(source_metrics,2); base_sp=_base_sp(source_metrics)
    sp=max(1e-5,min(0.95,base_sp*float(profile['sp_scale'])))
    lp=max(0.0,min(sp,sp*float(profile.get('lp_fraction',0.0))))
    return bounded(base_D*float(profile['d_scale']),float(profile.get('B',0.0)),sp,lp,1.0,str(profile.get('color_mode','independent')))


def plan_parameters(source_metrics:dict[str,Any],round_number:int)->list[GHSParameters]:
    """v1.2.4: preserve v1.2.2 chroma generation, reduce washout geometrically.

    Round 1 is exactly the successful v1.2.2 candidate family.  Round 2 uses three
    relative profiles selected by the installer from a bounded real-Siril grid around
    the proven broad Independent D~2.4 branch.  The search changes SP and low
    protection rather than discarding GHS energy.  Rounds 3-5 are deliberately tiny
    refinements and should be entered only when visual/numeric evidence says Round 2
    still needs work.
    """
    if round_number==1:
        sp=_base_sp(source_metrics); base_D=_base_D(source_metrics,1)
        variants=[
            (0.88,0.00,0.97,'independent'),
            (1.00,0.30,1.00,'human'),
            (1.10,0.60,1.03,'even'),
        ]
        return [bounded(base_D*d,b,sp*sm,0.0,1.0,cm) for d,b,sm,cm in variants]

    if round_number==2:
        if len(ROUND2_PROFILES)!=3:
            raise StretchError(f'ROUND2_PROFILES must contain exactly 3 entries, got {len(ROUND2_PROFILES)}')
        return [round2_parameters(source_metrics,p) for p in ROUND2_PROFILES]

    # If another cycle is genuinely needed, do not repeat a large luminance lift.
    mode=float(source_metrics['mode']); median=float(source_metrics['p50']); p95=float(source_metrics['p95'])
    pivot=max(0.10,min(0.70,max(mode*1.04,median*1.02,p95*0.82)))
    base_D=_base_D(source_metrics,round_number)
    variants=[
        (0.78,0.00,0.96,'independent'),
        (1.00,0.15,1.02,'human'),
        (1.12,0.35,1.08,'even'),
    ]
    return [bounded(base_D*d,b,pivot*sm,0.0,1.0,cm) for d,b,sm,cm in variants]


def bp_target_floor(round_number:int,index:int)->float|None:
    # Round 2 does not use a percentile floor.  It uses a fraction of the actual
    # post-GHS minimum so strict zero-new-clipping is satisfied by construction and
    # the BP can be as strong as safely possible without erasing real RGB data.
    if round_number==2:
        return None
    table={
        1:(0.16,0.13,0.10),
        3:(0.10,0.085,0.070),
        4:(0.09,0.075,0.060),
        5:(0.08,0.065,0.050),
    }
    return table[round_number][index]


def bp_policy(round_number:int,index:int,ghs_metrics:dict[str,Any])->dict[str,Any]:
    if round_number==2:
        profile=ROUND2_PROFILES[index]
        frac=float(profile.get('bp_min_fraction',0.985))
        frac=max(0.0,min(0.995,frac))
        minimum=max(0.0,float(ghs_metrics.get('minimum') or 0.0))
        proposed=min(0.35,minimum*frac)
        return {"strategy":"fraction_of_actual_minimum","target_floor":None,
                "minimum_fraction":frac,"proposed":float(proposed)}
    floor=bp_target_floor(round_number,index)
    proposed=bp_for_floor(float(ghs_metrics['p0.1']),float(floor)) if floor is not None else 0.0
    return {"strategy":"lifted_percentile_floor","target_floor":floor,
            "minimum_fraction":None,"proposed":float(proposed)}


def ghs_backoff_factors()->tuple[float,...]:
    # The GHS operation itself can create clipping; BP backoff cannot repair that.
    # Therefore find a safe GHS first, then solve BP from that safe result.
    return (1.0,0.84,0.70,0.58,0.48,0.39,0.31,0.24,0.18,0.13,0.09)


def ghs_safety_reasons(m:dict[str,Any])->list[str]:
    reasons=[]
    if m.get('new_low_clip_fraction',0.0)>0.0:
        reasons.append('GHS introduced shadow clipping')
    if m.get('new_high_clip_fraction',0.0)>0.0:
        reasons.append('GHS introduced highlight clipping')
    # Quantile headroom is separate from exact clipping.
    if m['p99.9']>0.985:
        reasons.append('GHS left insufficient upper-tail headroom')
    return reasons


def bp_safety_reasons(m:dict[str,Any])->list[str]:
    reasons=[]
    if m.get('new_low_clip_fraction',0.0)>0.0:
        reasons.append('BP introduced shadow clipping')
    if m.get('new_high_clip_fraction',0.0)>0.0:
        reasons.append('BP introduced highlight clipping')
    if m['p99.9']>0.985: reasons.append('insufficient upper-tail headroom')
    return reasons


def candidate_eligibility(m:dict[str,Any],bp:float)->tuple[bool,list[str]]:
    reasons=[]
    if m['finite_fraction']!=1.0: reasons.append('non-finite pixels')
    reasons.extend(bp_safety_reasons(m))
    # Pearson correlation is not a valid hard structure gate for a deliberately
    # non-linear monotonic stretch. Use rank correlation for the hard gate and keep
    # Pearson only as a diagnostic metric.
    if m.get('luma_rank_correlation',1.0)<0.985: reasons.append('luminance ordering changed excessively')
    if m.get('saturation_retention',1.0)<0.72: reasons.append('relative saturation retention too low')
    if m.get('chroma_retention',1.0)<0.72: reasons.append('relative chroma retention too low')
    # A zero BP is valid when GHS already leaves an appropriate low-end placement.
    # The successful manual workflow also ended with a GHS touch without a following BP.
    return not reasons,reasons


def _band_score(v:float,center:float,half_width:float)->float:
    return max(0.0,1.0-abs(float(v)-center)/half_width)


def score_candidate(m:dict[str,Any],source_metrics:dict[str,Any])->float:
    """Reward real channel separation and penalize luminance-only expansion early."""
    src_spread=max(1e-6,float(source_metrics['spread_p95_p05']))
    gain=max(0.0,float(m['spread_p95_p05'])-src_spread)
    gain_score=min(1.0,gain/max(0.10,src_spread*1.35))
    peak_score=_band_score(m['mode'],0.20,0.20)
    median_score=_band_score(m['p50'],0.215,0.18)
    upper_score=_band_score(m['p99'],0.47,0.34)
    highlight=max(0.0,min(1.0,(0.92-m['p99.9'])/0.30))
    retention=min(1.15,max(0.0,min(m.get('saturation_retention',1.0),m.get('chroma_retention',1.0))))
    sat_score=min(1.0,float(m.get('saturation_median') or 0.0)/0.31)
    chroma_score=min(1.0,float(m.get('chroma_median') or 0.0)/0.082)
    contrast_score=min(1.0,float(m.get('color_contrast_index') or 0.0)/0.39)
    # A candidate can be completely unclipped and still be visibly washed.  Start
    # penalizing before the old v1.2.2 median/mode neighborhood, and penalize loss
    # of normalized colour contrast more strongly than lack of extra brightness.
    penalty=(
        max(0.0,m['p50']-0.275)*7.0 +
        max(0.0,m['mode']-0.275)*4.0 +
        max(0.0,m['p99.9']-0.72)*4.5 +
        max(0.0,m['p99']-0.62)*2.5
    )
    return float(
        0.28*gain_score + 0.54*peak_score + 0.68*median_score +
        0.42*upper_score + 0.36*highlight + 0.44*sat_score +
        0.54*chroma_score + 0.82*contrast_score + 0.08*retention - penalty
    )

def _relative01(v:float,lo:float,hi:float)->float:
    if hi<=lo+1e-12: return 0.5
    return max(0.0,min(1.0,(float(v)-lo)/(hi-lo)))

def add_round_relative_color_scores(candidates:list[dict[str,Any]])->None:
    eligible=[c for c in candidates if c.get('eligible') and c.get('metrics')]
    if not eligible: return
    sats=[float(c['metrics']['saturation_median']) for c in eligible]
    chromas=[float(c['metrics']['chroma_median']) for c in eligible]
    ccis=[float(c['metrics'].get('color_contrast_index') or 0.0) for c in eligible]
    slo,shi=min(sats),max(sats); clo,chi=min(chromas),max(chromas); xlo,xhi=min(ccis),max(ccis)
    for c in eligible:
        m=c['metrics']
        srel=_relative01(m['saturation_median'],slo,shi)
        crel=_relative01(m['chroma_median'],clo,chi)
        xrel=_relative01(m.get('color_contrast_index',0.0),xlo,xhi)
        color_relative=0.30*srel+0.25*crel+0.45*xrel
        c['round_relative_color_score']=float(color_relative)
        c['technical_score']=float(c['technical_score']+0.64*color_relative)
        atomic_json(Path(c['output']['path']).parent/'candidate-result.json',c)

def execute_candidate(source:Path,round_dir:Path,round_number:int,index:int,params:GHSParameters,timeout:int)->dict[str,Any]:
    cid=f"candidate-{index:02d}"; root=round_dir/cid; root.mkdir(parents=True,exist_ok=False)
    shutil.copy2(source,root/'input.fit')
    final=root/'after-ghs-bp.fit'; preview=root/'after-ghs-bp.png'

    # Phase 1: find a real-Siril GHS that is safe before attempting BP. In v1.1.1
    # GHS was executed once and BP was repeatedly backed off even when GHS itself
    # had already clipped highlights; that can never repair the clipped GHS result.
    ghs_attempts=[]; selected_params=None; ghs_out=None; gm=None; ghs_run=None
    for n,factor in enumerate(ghs_backoff_factors()):
        trial_params=bounded(params.D*factor,params.B,params.SP,params.LP,params.HP,params.color_mode)
        trial=root/f'after-ghs-trial-{n:02d}.fit'
        try:
            run=run_siril(root,["requires 1.4.4","load input.fit",trial_params.command(),f"save {trial.name}","close"],timeout)
        except StretchError as e:
            # One experimental candidate must not abort the whole round. Record
            # the exact command failure and continue its bounded GHS backoff.
            ghs_attempts.append({"factor":factor,"params":trial_params.as_dict(),"command":trial_params.command(),
                                 "safe":False,"command_failed":True,"safety_reasons":["Siril GHS command failed"],
                                 "error":str(e),"metrics":None})
            continue
        mm=metrics(trial,reference=source)
        safety=ghs_safety_reasons(mm)
        ghs_attempts.append({"factor":factor,"params":trial_params.as_dict(),"command":trial_params.command(),
                             "safe":not safety,"command_failed":False,"safety_reasons":safety,
                             "metrics":{k:mm.get(k) for k in ('minimum','maximum','mode','p0.1','p1','p50','p95','p99','p99.9','low_clip_delta','high_clip_delta','new_low_clip_fraction','new_high_clip_fraction','channel_new_low_clip_fraction','channel_new_high_clip_fraction','luma_correlation','luma_rank_correlation','saturation_median','saturation_p75','chroma_median','chroma_p75','saturation_retention','chroma_retention','color_contrast_index','color_contrast_p75','color_contrast_retention')}})
        if not safety:
            selected_params=trial_params; ghs_out=trial; gm=mm; ghs_run=run
            break

    if selected_params is None:
        # Candidate remains in the diagnostic panel but cannot proceed to BP.
        last=ghs_attempts[-1]
        all_command_failures=all(bool(a.get('command_failed')) for a in ghs_attempts)
        reason="all bounded Siril GHS commands failed" if all_command_failures else "no safe GHS strength found"
        rec={"candidate":cid,"round":round_number,"ghs_requested":params.as_dict(),"ghs":last['params'],
             "ghs_command":last.get('command') or bounded(**last['params']).command(),"ghs_attempts":ghs_attempts,
             "bp_proposed":0.0,"bp":0.0,"bp_target_floor":bp_target_floor(round_number,index),
             "bp_strategy":"fraction_of_actual_minimum" if round_number==2 else "lifted_percentile_floor",
             "bp_min_fraction":float(ROUND2_PROFILES[index].get('bp_min_fraction',0.985)) if round_number==2 else None,
             "bp_command":bp_command(0.0),"bp_attempts":[],
             "ghs_metrics":last.get('metrics'),"metrics":last.get('metrics'),"eligible":False,
             "rejection_reasons":[reason]+list(last.get('safety_reasons') or []),"technical_score":-1e9,
             "output":None,"preview":None,"ghs_run":None,"bp_run":None,"preview_run":None}
        atomic_json(root/'candidate-result.json',rec); return rec

    # Preserve the selected safe GHS under a stable filename for BP trials.
    stable_ghs=root/'after-ghs.fit'; shutil.copy2(ghs_out,stable_ghs)

    # Phase 2: perform an *incremental* BP reset on the already-safe GHS result.
    # Do not maximize the safe BP. The target floor intentionally remains lifted
    # so later rounds still have room to widen the histogram and retain colour.
    policy=bp_policy(round_number,index,gm)
    floor=policy['target_floor']; bp_min_fraction=policy['minimum_fraction']; bp_strategy=policy['strategy']
    proposed=float(policy['proposed'])
    attempts=[]; selected_bp=None; fm=None; bp_run=None
    if proposed<=1e-10:
        selected_bp=0.0; fm=gm; bp_run={"exit_code":None,"diagnostic":"BP not required for proposed lifted floor"}
        shutil.copy2(stable_ghs,final)
    for n,factor in enumerate(backoff_factors() if proposed>1e-10 else ()):
        bp=proposed*factor
        trial=root/f'after-ghs-bp-trial-{n:02d}.fit'
        run=run_siril(root,["requires 1.4.4","load after-ghs.fit",bp_command(bp),f"save {trial.name}","close"],timeout)
        mm=metrics(trial,reference=source)
        safety=bp_safety_reasons(mm)
        attempts.append({"factor":factor,"bp":bp,"safe":not safety,"safety_reasons":safety,
                         "metrics":{k:mm.get(k) for k in ('minimum','maximum','p0.1','p1','p50','p95','p99','p99.9','low_clip_delta','high_clip_delta','new_low_clip_fraction','new_high_clip_fraction','channel_new_low_clip_fraction','channel_new_high_clip_fraction','luma_correlation','luma_rank_correlation','saturation_median','saturation_p75','chroma_median','chroma_p75','saturation_retention','chroma_retention','color_contrast_index','color_contrast_p75','color_contrast_retention')}})
        if not safety:
            selected_bp=bp; fm=mm; bp_run=run; shutil.copy2(trial,final); break
    if selected_bp is None:
        selected_bp=0.0; fm=metrics(stable_ghs,reference=source); bp_run={"exit_code":None,"diagnostic":"no safe BP trial"}; shutil.copy2(stable_ghs,final)

    preview_run=run_siril(root,["requires 1.4.4","load after-ghs-bp.fit","savepng after-ghs-bp","close"],timeout)
    ok,reasons=candidate_eligibility(fm,selected_bp)
    score=score_candidate(fm,metrics(source)) if ok else -1e9
    rec={"candidate":cid,"round":round_number,"ghs_requested":params.as_dict(),"ghs":selected_params.as_dict(),"ghs_command":selected_params.command(),"ghs_attempts":ghs_attempts,
         "bp_proposed":proposed,"bp":selected_bp,"bp_target_floor":floor,"bp_strategy":bp_strategy,"bp_min_fraction":bp_min_fraction,"bp_command":bp_command(selected_bp),"bp_attempts":attempts,
         "ghs_metrics":gm,"metrics":fm,"eligible":ok,"rejection_reasons":reasons,"technical_score":score,
         "output":{"path":str(final),"sha256":sha256_file(final)},"preview":{"path":str(preview),"sha256":sha256_file(preview)},
         "ghs_run":ghs_run,"bp_run":bp_run,"preview_run":preview_run}
    atomic_json(root/'candidate-result.json',rec); return rec

def rejection_summary(candidates:list[dict[str,Any]])->list[dict[str,Any]]:
    out=[]
    for c in candidates:
        m=c.get('metrics') or {}
        out.append({"candidate":c['candidate'],"eligible":c['eligible'],"rejection_reasons":c['rejection_reasons'],
                    "ghs_requested":c.get('ghs_requested'),"ghs":c['ghs'],"ghs_attempts":c.get('ghs_attempts',[]),
                    "bp_proposed":c['bp_proposed'],"bp_selected":c['bp'],
                    "metrics":{k:m.get(k) for k in ('minimum','maximum','mode','p0.1','p1','p5','p50','p95','p99','p99.9','spread_p95_p05','spread_p99_p01','low_clip_fraction','high_clip_fraction','low_clip_delta','high_clip_delta','new_low_clip_fraction','new_high_clip_fraction','channel_new_low_clip_fraction','channel_new_high_clip_fraction','luma_correlation','luma_rank_correlation','saturation_median','saturation_p75','chroma_median','chroma_p75','saturation_retention','chroma_retention','color_contrast_index','color_contrast_p75','color_contrast_retention')},
                    "bp_attempts":c['bp_attempts']})
    return out

def run_round(source:Path,run_root:Path,round_number:int,timeout:int)->dict[str,Any]:
    rd=run_root/f"round-{round_number:02d}"; rd.mkdir(parents=True,exist_ok=False)
    sm=metrics(source); params=plan_parameters(sm,round_number)
    candidates=[execute_candidate(source,rd,round_number,i,p,timeout) for i,p in enumerate(params)]
    add_round_relative_color_scores(candidates)
    eligible=[c for c in candidates if c['eligible']]
    recommended=max(eligible,key=lambda c:c['technical_score'])['candidate'] if eligible else None
    result={"round":round_number,"source":{"path":str(source),"sha256":sha256_file(source),"metrics":sm},
            "candidates":candidates,"eligible_candidates":[c['candidate'] for c in eligible],"recommended_candidate":recommended,
            "rejection_summary":rejection_summary(candidates)}
    atomic_json(rd/'round-result.json',result)
    if not eligible:
        raise StretchError(f"Round {round_number} produced no technically safe candidate. Diagnostics: "+json.dumps(result['rejection_summary'],sort_keys=True))
    return result

def self_test(root:Path,timeout:int=600)->dict[str,Any]:
    root.mkdir(parents=True,exist_ok=False)
    h,w=96,112; y,x=np.mgrid[0:h,0:w]
    neb=np.exp(-(((x-w*.52)/24)**2+((y-h*.50)/20)**2)); ridge=np.exp(-(((x-w*.44)/10)**2+((y-h*.58)/23)**2))
    r=.020+.055*neb+.015*ridge; g=.022+.075*neb+.020*ridge; b=.018+.065*neb+.015*np.roll(ridge,7,axis=1)
    src=root/'synthetic.fit'; fits.PrimaryHDU(np.stack([r,g,b]).astype(np.float32)).writeto(src)
    results=[]
    # Exercise all three Siril colour models used by production planning.
    mechanical=((0.55, 0.00,0.030,0.002,'independent'),
                (0.70, 0.30,0.035,0.003,'human'),
                (0.85, 0.60,0.040,0.004,'even'))
    for i,(D,B,SP,bp,mode_name) in enumerate(mechanical):
        c=root/f'mechanical-{i:02d}'; c.mkdir()
        shutil.copy2(src,c/'input.fit')
        p=bounded(D,B,SP,0.0,1.0,mode_name)
        g=run_siril(c,["requires 1.4.4","load input.fit",p.command(),"save after-ghs.fit","close"],timeout)
        bpr=run_siril(c,["requires 1.4.4","load after-ghs.fit",bp_command(bp),"save after-ghs-bp.fit","close"],timeout)
        m=metrics(c/'after-ghs-bp.fit',reference=src,stride=1)
        if m['finite_fraction']!=1.0 or m['high_clip_fraction']>0.0:
            raise StretchError(f"Mechanical self-test candidate {i} failed: "+json.dumps(m,sort_keys=True))
        if sha256_file(c/'after-ghs-bp.fit')==sha256_file(src):
            raise StretchError(f"Mechanical self-test candidate {i} did not change the source")
        results.append({"candidate":i,"ght_exit":g['exit_code'],"linstretch_exit":bpr['exit_code'],"metrics":m})
    return {"status":"success","version":VERSION,"real_siril":True,"mechanical_candidate_count":len(results),
            "all_siril_steps_succeeded":True,"candidates":results}
