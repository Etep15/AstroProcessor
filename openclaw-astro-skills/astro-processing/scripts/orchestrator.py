#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, os, shutil, tempfile
from pathlib import Path
from typing import Any

VERSION="2.0.0"
WORKSPACE=Path("/home/peter/.openclaw/workspace/agents/codewarrior")
PROJECTS=WORKSPACE/"Projects"
SKILLS=WORKSPACE/"skills"
STAGES=[
 "siril-mono-preprocessing",
 "siril-master-alignment",
 "siril-mono-background-cleanup",
 "siril-mono-linear-denoise",
 "siril-sho-combination",
 "siril-background-neutralization",
 "siril-starnet-removal",
 "siril-sho-channel-balance",
 "siril-stretch",
 "siril-green-reduction",
 "siril-saturation",
 "siril-star-processing",
 "siril-star-recombination",
]
FINAL=STAGES[-1]

class Err(RuntimeError): pass

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def stamp(): return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
def sha(p:Path):
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def safe_project(s:str):
    s=s.strip()
    if not s or "/" in s or "\\" in s or s in {".",".."} or "\0" in s: raise Err("Invalid project name.")
    return s

def pdir(project): return PROJECTS/safe_project(project)
def spath(project): return pdir(project)/"processing-state.json"

def dump(path:Path,obj:dict):
    path.parent.mkdir(parents=True,exist_ok=True)
    t=path.with_name(f".{path.name}.tmp-{os.getpid()}")
    t.write_text(json.dumps(obj,indent=2,sort_keys=True)+"\n",encoding="utf-8"); t.replace(path)

def write(path:Path,text:str):
    path.parent.mkdir(parents=True,exist_ok=True)
    t=path.with_name(f".{path.name}.tmp-{os.getpid()}")
    t.write_text(text,encoding="utf-8"); t.replace(path)

def load(path:Path):
    try: x=json.loads(path.read_text(encoding="utf-8"))
    except Exception as e: raise Err(f"Could not read JSON {path}: {e}") from e
    if not isinstance(x,dict): raise Err(f"Expected JSON object: {path}")
    return x

def state(project):
    f=spath(project)
    if not f.is_file(): raise Err(f"No astro-processing v2 state exists for {project}.")
    x=load(f)
    if x.get("orchestrator_version")!=VERSION: raise Err("processing-state.json belongs to a different orchestrator version.")
    return x

def save(x):
    x["updated_at_utc"]=now(); dump(Path(x["state_path"]),x)

def skeleton():
    return {s:{"status":"pending","action":None,"started_at_utc":None,"completed_at_utc":None,
               "version":None,"selected":None,"manifest":None,"outputs":[],"note":None,
               "manifest_summary":None} for s in STAGES}

def within(root:Path,p:Path):
    try: p.resolve().relative_to(root.resolve())
    except ValueError as e: raise Err(f"Evidence must be inside the project: {p}") from e

def manifest_summary(m:dict):
    keys=("status","version","helper_version","orchestration_version","processing_engine_version",
          "selected_candidate","selected_candidates","recommended_candidate","recommended_candidates",
          "next_stage","final_processing_complete","visual_review_completed","stage_order")
    return {k:m[k] for k in keys if k in m}

def next_name(x):
    for s in STAGES:
        if x["stages"][s]["status"] not in ("ready","skipped"): return s
    return None

def delegation(x,s):
    f=SKILLS/s/"SKILL.md"
    if not f.is_file(): return {"status":"blocked","stage":s,"error":f"Required child skill is missing: {f}"}
    return {"status":"delegate","orchestrator_version":VERSION,"project":x["project"],"run_id":x["run_id"],
            "stage":s,"skill_md":str(f),
            "instruction":"Read this exact installed SKILL.md and let the child skill own the complete stage, including exact-path visual review and autonomous selection.",
            "rerun_policy":"Skip a current valid result. If it is obsolete only because this same full run replaced its upstream, the full-run authorization permits the child skill's documented fresh-confirmation path."}

def report_obj(x,status):
    final=x["stages"][FINAL]
    return {"schema_version":2,"orchestrator_version":VERSION,"overall_status":status,
            "project":x["project"],"target":x.get("target"),"pipeline":"SHO",
            "project_path":x["project_path"],
            "source":{"project":x["source_project"],"root":x["source_root"],"type":x["source_type"]},
            "run_id":x["run_id"],"started_at_utc":x["started_at_utc"],
            "completed_at_utc":x.get("completed_at_utc"),"updated_at_utc":x["updated_at_utc"],
            "astroprocessor_setup":x.get("astroprocessor_setup"),"stages":x["stages"],
            "warnings":x.get("warnings",[]),"blocked_reason":x.get("blocked_reason"),
            "final_manifest":final.get("manifest"),"final_outputs":final.get("outputs"),
            "final_selection":final.get("selected")}

def report_md(r):
    L=[f"# Full Astro Processing Report — {r['project']}","",
       f"- **Status:** `{r['overall_status']}`",f"- **Pipeline:** `SHO`",
       f"- **Orchestrator:** `{VERSION}`",f"- **Run ID:** `{r['run_id']}`",
       f"- **Source project:** `{r['source']['project']}`",f"- **Source root:** `{r['source']['root']}`",
       f"- **Source type:** `{r['source']['type']}`","","## AstroProcessor setup",""]
    setup=r.get("astroprocessor_setup") or {}
    L += [f"- new-project exit: `{setup.get('new_project_exit')}`",
          f"- copy exit: `{setup.get('copy_exit')}`",
          f"- prepare exit: `{setup.get('prepare_exit')}`"]
    if setup.get("note"): L.append(f"- note: {setup['note']}")
    L += ["","## Processing stages","","| Stage | Status | Action | Version | Selection |",
          "|---|---|---|---|---|"]
    for s in STAGES:
        q=r["stages"][s]; sel=q.get("selected")
        if isinstance(sel,(dict,list)): sel=json.dumps(sel,sort_keys=True)
        L.append(f"| `{s}` | `{q.get('status')}` | `{q.get('action')}` | `{q.get('version')}` | `{sel}` |")
        if q.get("manifest"): L += [f"\nManifest: `{q['manifest']['path']}`  ",f"SHA-256: `{q['manifest']['sha256']}`"]
        for o in q.get("outputs") or []: L += [f"\nOutput: `{o['path']}`  ",f"SHA-256: `{o['sha256']}`"]
        if q.get("note"): L.append(f"\nNote: {q['note']}")
    if r.get("blocked_reason"): L += ["","## Blocker","",r["blocked_reason"]]
    L += ["","## Final output",""]
    if r.get("final_manifest"): L += [f"- Manifest: `{r['final_manifest']['path']}`",f"- Manifest SHA-256: `{r['final_manifest']['sha256']}`"]
    for o in r.get("final_outputs") or []: L.append(f"- `{o['path']}` — `{o['sha256']}`")
    if not r.get("final_outputs"): L.append("- No final output recorded yet.")
    return "\n".join(L)+"\n"

def write_report(x,status):
    root=Path(x["project_path"]); reports=root/"processing"/"reports"; reports.mkdir(parents=True,exist_ok=True)
    r=report_obj(x,status)
    runj=reports/f"full-processing-{x['run_id']}.json"; runm=reports/f"full-processing-{x['run_id']}.md"
    canj=root/"processing"/"full-processing-report.json"; canm=root/"processing"/"full-processing-report.md"
    dump(runj,r); write(runm,report_md(r))
    if canj.is_file():
        prior=reports/f"prior-full-processing-report-{stamp()}.json"
        if not prior.exists(): shutil.copy2(canj,prior)
    if canm.is_file():
        prior=reports/f"prior-full-processing-report-{stamp()}.md"
        if not prior.exists(): shutil.copy2(canm,prior)
    shutil.copy2(runj,canj); shutil.copy2(runm,canm)
    x["report"]={"json":str(canj),"markdown":str(canm),"run_json":str(runj),"run_markdown":str(runm)}
    save(x); return x["report"]

def cmd_plan(a):
    p=safe_project(a.project)
    print(json.dumps({"status":"plan","version":VERSION,"target_agnostic":True,"pipeline":"SHO",
      "project":p,"target":a.target or p,"source_project":a.source_project or p,
      "source_root":a.source_root,"source_type":a.source_type,"stages":STAGES,"final_report":True},indent=2)); return 0

def cmd_begin(a):
    p=safe_project(a.project); root=pdir(p); f=spath(p)
    if f.is_file():
        x=load(f)
        if x.get("orchestrator_version")==VERSION:
            if x.get("status")=="complete":
                print(json.dumps({"status":"confirmation_required","project":p,
                 "question":f"{p} already has a completed full-processing run. Do you want to fully reprocess it?"},indent=2)); return 0
            print(json.dumps({"status":"resume","project":p,"run_id":x.get("run_id"),
             "current_stage":x.get("current_stage"),"blocked_reason":x.get("blocked_reason")},indent=2)); return 0
    final=root/"processing"/"star-recombination"/"star-recombination-manifest.json"
    if final.is_file():
        m=load(final)
        if m.get("status")=="ready" and m.get("final_processing_complete") is True:
            print(json.dumps({"status":"confirmation_required","project":p,
             "question":f"{p} already has a completed star-recombination result. Do you want to fully reprocess it from source?",
             "existing_final_manifest":str(final)},indent=2)); return 0
    if not root.is_dir(): raise Err(f"Project does not exist after AstroProcessor setup: {root}")
    run=f"full-{stamp()}-{os.getpid()}"
    x={"schema_version":2,"orchestrator_version":VERSION,"status":"active","run_id":run,
       "project":p,"target":a.target or p,"project_path":str(root),
       "source_project":a.source_project or p,"source_root":a.source_root,"source_type":a.source_type,
       "started_at_utc":now(),"updated_at_utc":now(),"completed_at_utc":None,
       "state_path":str(f),"run_root":str(root/".astro-processing"/run),
       "astroprocessor_setup":{"new_project_exit":a.new_project_exit,"copy_exit":a.copy_exit,
          "prepare_exit":a.prepare_exit,"note":a.setup_note},
       "stages":skeleton(),"current_stage":STAGES[0],"blocked_reason":None,"warnings":[],"report":None}
    save(x)
    print(json.dumps({"status":"ready_for_pipeline","project":p,"run_id":run,"next":delegation(x,STAGES[0])},indent=2)); return 0

def cmd_next(a):
    x=state(a.project)
    if x["status"]=="blocked":
        print(json.dumps({"status":"blocked","project":x["project"],"stage":x.get("current_stage"),"reason":x.get("blocked_reason"),"report":x.get("report")},indent=2)); return 2
    if x["status"]=="complete":
        print(json.dumps({"status":"complete","project":x["project"],"report":x.get("report")},indent=2)); return 0
    s=next_name(x)
    if s is None:
        print(json.dumps({"status":"ready_to_finish","project":x["project"]},indent=2)); return 0
    q=x["stages"][s]
    if q["status"]=="pending": q["status"]="delegated"; q["started_at_utc"]=now(); x["current_stage"]=s; save(x)
    d=delegation(x,s)
    if d["status"]=="blocked":
        q["status"]="blocked"; x["status"]="blocked"; x["blocked_reason"]=d["error"]; save(x); d["report"]=write_report(x,"blocked")
        print(json.dumps(d,indent=2)); return 2
    print(json.dumps(d,indent=2)); return 0

def cmd_record(a):
    x=state(a.project); s=a.stage
    idx=STAGES.index(s)
    for prev in STAGES[:idx]:
        if x["stages"][prev]["status"] not in ("ready","skipped"): raise Err(f"Prior stage is incomplete: {prev}")
    q=x["stages"][s]
    if a.status=="blocked":
        q.update(status="blocked",action="blocked",completed_at_utc=now(),note=a.note)
        x["status"]="blocked"; x["current_stage"]=s; x["blocked_reason"]=a.note or f"{s} blocked."
        save(x); rep=write_report(x,"blocked")
        print(json.dumps({"status":"blocked","stage":s,"report":rep},indent=2)); return 2
    root=Path(x["project_path"]); mi=None; ms=None
    if a.manifest:
        mp=Path(a.manifest); within(root,mp)
        if not mp.is_file(): raise Err(f"Manifest missing: {mp}")
        m=load(mp)
        if m.get("status") not in (None,"ready"): raise Err(f"Manifest status is not ready: {m.get('status')}")
        mi={"path":str(mp),"sha256":sha(mp)}; ms=manifest_summary(m)
        if s==FINAL and a.status=="ready" and m.get("final_processing_complete") is not True: raise Err("Final manifest does not report final_processing_complete: true.")
    outs=[]
    for v in a.output or []:
        op=Path(v); within(root,op)
        if not op.is_file(): raise Err(f"Output missing: {op}")
        outs.append({"path":str(op),"sha256":sha(op),"size":op.stat().st_size})
    ver=a.version
    sel=a.selected
    if ms:
        ver=ver or ms.get("orchestration_version") or ms.get("version") or ms.get("helper_version")
        sel=sel or ms.get("selected_candidate") or ms.get("selected_candidates")
    q.update(status=a.status,action=("skipped_current" if a.status=="skipped" else "executed"),
             completed_at_utc=now(),version=ver,selected=sel,manifest=mi,outputs=outs,note=a.note,manifest_summary=ms)
    x["status"]="active"; x["blocked_reason"]=None; x["current_stage"]=(STAGES[idx+1] if idx+1<len(STAGES) else None); save(x)
    print(json.dumps({"status":"recorded","stage":s,"stage_status":a.status,"version":ver,"selected":sel,
      "next":(delegation(x,x["current_stage"]) if x["current_stage"] else {"status":"ready_to_finish"})},indent=2)); return 0

def cmd_finish(a):
    x=state(a.project)
    inc=[s for s in STAGES if x["stages"][s]["status"] not in ("ready","skipped")]
    if inc: raise Err(f"Cannot finish; incomplete stages: {inc}")
    if x["stages"][FINAL].get("manifest") is None: raise Err("Final star-recombination manifest was not recorded.")
    x["status"]="complete"; x["current_stage"]=None; x["blocked_reason"]=None; x["completed_at_utc"]=now(); save(x)
    rep=write_report(x,"complete")
    print(json.dumps({"status":"complete","version":VERSION,"project":x["project"],
      "final_manifest":x["stages"][FINAL]["manifest"],"final_outputs":x["stages"][FINAL]["outputs"],
      "final_selection":x["stages"][FINAL]["selected"],"report":rep},indent=2)); return 0

def cmd_status(a):
    x=state(a.project)
    print(json.dumps({"status":x["status"],"version":VERSION,"project":x["project"],"run_id":x["run_id"],
      "current_stage":x.get("current_stage"),"blocked_reason":x.get("blocked_reason"),
      "stages":{s:{"status":x["stages"][s]["status"],"action":x["stages"][s]["action"],
                   "version":x["stages"][s]["version"],"selected":x["stages"][s]["selected"]} for s in STAGES},
      "report":x.get("report")},indent=2)); return 0

def cmd_report(a):
    x=state(a.project); st="complete" if x["status"]=="complete" else ("blocked" if x["status"]=="blocked" else "in_progress")
    print(json.dumps({"status":"report_written","report":write_report(x,st)},indent=2)); return 0

def cmd_self(a):
    root=Path(a.root) if a.root else Path(tempfile.mkdtemp(prefix="astro-processing-v2-selftest-"))
    proj=root/"Synthetic Target"; (proj/"processing").mkdir(parents=True,exist_ok=True)
    x={"schema_version":2,"orchestrator_version":VERSION,"status":"active","run_id":"synthetic-run",
       "project":"Synthetic Target","target":"Synthetic Target","project_path":str(proj),
       "source_project":"Synthetic Source","source_root":"/mnt/example","source_type":"autorun",
       "started_at_utc":now(),"updated_at_utc":now(),"completed_at_utc":now(),
       "state_path":str(proj/"processing-state.json"),"run_root":str(proj/".astro-processing/synthetic-run"),
       "astroprocessor_setup":{"new_project_exit":0,"copy_exit":0,"prepare_exit":0,"note":"synthetic"},
       "stages":skeleton(),"current_stage":None,"blocked_reason":None,"warnings":[],"report":None}
    for s in STAGES:
        d=proj/"processing"/s; d.mkdir(parents=True,exist_ok=True)
        o=d/"output.fit"; o.write_bytes((s+"\n").encode())
        m={"status":"ready","version":"test","selected_candidate":"candidate-test","output":{"path":str(o),"sha256":sha(o)}}
        if s==FINAL: m["final_processing_complete"]=True
        mf=d/"manifest.json"; dump(mf,m)
        x["stages"][s].update(status="ready",action="executed",version="test",selected="candidate-test",
          manifest={"path":str(mf),"sha256":sha(mf)},outputs=[{"path":str(o),"sha256":sha(o),"size":o.stat().st_size}],
          manifest_summary=manifest_summary(m),completed_at_utc=now())
    x["status"]="complete"; dump(Path(x["state_path"]),x); rep=write_report(x,"complete")
    assert len(STAGES)==13 and Path(rep["json"]).is_file() and Path(rep["markdown"]).is_file()
    print(json.dumps({"status":"success","version":VERSION,"target_agnostic":True,"pipeline":"SHO",
      "stage_count":len(STAGES),"stage_order":STAGES,"durable_state":True,"resume_supported":True,
      "final_report_json_and_markdown":True,"child_skills_own_processing_and_visual_review":True,
      "named_stage_routing_preserved":True,"no_production_processing":True},indent=2)); return 0

def parser():
    p=argparse.ArgumentParser(); p.add_argument("--version",action="version",version=VERSION)
    sub=p.add_subparsers(dest="cmd",required=True)
    x=sub.add_parser("plan"); x.add_argument("--project",required=True); x.add_argument("--target"); x.add_argument("--source-project"); x.add_argument("--source-root",required=True); x.add_argument("--source-type",required=True); x.set_defaults(fn=cmd_plan)
    x=sub.add_parser("begin"); x.add_argument("--project",required=True); x.add_argument("--target"); x.add_argument("--source-project"); x.add_argument("--source-root",required=True); x.add_argument("--source-type",required=True); x.add_argument("--new-project-exit",type=int,required=True); x.add_argument("--copy-exit",type=int,required=True); x.add_argument("--prepare-exit",type=int,required=True); x.add_argument("--setup-note"); x.set_defaults(fn=cmd_begin)
    x=sub.add_parser("next"); x.add_argument("--project",required=True); x.set_defaults(fn=cmd_next)
    x=sub.add_parser("record-stage"); x.add_argument("--project",required=True); x.add_argument("--stage",choices=STAGES,required=True); x.add_argument("--status",choices=["ready","skipped","blocked"],required=True); x.add_argument("--manifest"); x.add_argument("--output",action="append"); x.add_argument("--version"); x.add_argument("--selected"); x.add_argument("--note"); x.set_defaults(fn=cmd_record)
    x=sub.add_parser("finish"); x.add_argument("--project",required=True); x.set_defaults(fn=cmd_finish)
    x=sub.add_parser("status"); x.add_argument("--project",required=True); x.set_defaults(fn=cmd_status)
    x=sub.add_parser("report"); x.add_argument("--project",required=True); x.set_defaults(fn=cmd_report)
    x=sub.add_parser("self-test"); x.add_argument("--root"); x.set_defaults(fn=cmd_self)
    return p

def main():
    a=parser().parse_args()
    try: return int(a.fn(a))
    except Err as e: print(json.dumps({"status":"blocked","version":VERSION,"error":str(e)},indent=2)); return 2
    except Exception as e: print(json.dumps({"status":"blocked","version":VERSION,"error":f"Unexpected internal error: {type(e).__name__}: {e}"},indent=2)); return 3
if __name__=="__main__": raise SystemExit(main())

