#!/usr/bin/env python3
"""
Clip Editor — self-driving local app.

Drop a long video + its transcript JSON. The app picks + scores the best clips
itself (Claude API), cuts them 9:16, burns Hinglish captions, and shows them
with per-clip re-cut + rating (it learns from your ratings next time).

Run:  python app.py   (or double-click start.bat)   ->  http://127.0.0.1:5000
"""

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid

from flask import (Flask, jsonify, render_template_string, request,
                   send_from_directory)

import brain
import graphics

APP_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(APP_DIR, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024  # 8 GB


def _status_path(d): return os.path.join(d, "status.json")


def _read_status(job_dir):
    try:
        with open(_status_path(job_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"status": "unknown", "stage": "", "message": "", "clips": [], "cost": None}


def _write_status(job_dir, **kw):
    st = _read_status(job_dir)
    st.update(kw)
    with open(_status_path(job_dir), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _run(cmd_name, job_dir, *extra):
    p = subprocess.run(
        [sys.executable, os.path.join(APP_DIR, "job_tools.py"), cmd_name, job_dir, *extra],
        cwd=APP_DIR, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd_name} failed: {p.stderr[-800:] or p.stdout[-800:]}")


def _stem(job_dir):
    for n in os.listdir(job_dir):
        if os.path.splitext(n)[1].lower() in VIDEO_EXTS:
            return os.path.splitext(n)[0]
    return None


def _find_video(job_dir):
    for n in os.listdir(job_dir):
        if os.path.splitext(n)[1].lower() in VIDEO_EXTS:
            return os.path.join(job_dir, n)
    return None


def _add_cost(job_dir, extra_usd):
    st = _read_status(job_dir)
    cost = dict(st.get("cost") or {})
    cost["cost_usd"] = round((cost.get("cost_usd") or 0) + (extra_usd or 0), 4)
    _write_status(job_dir, cost=cost)


def _annotate_graphics(job_dir, only=None):
    """Detect on-screen graphics per clip and write fit-windows into clips.json."""
    video = _find_video(job_dir)
    if not video:
        return
    cpath = os.path.join(job_dir, "clips.json")
    with open(cpath, encoding="utf-8-sig") as f:
        spec = json.load(f)
    total = 0.0
    for c in spec.get("clips", []):
        if only and c.get("id") != only:
            continue
        try:
            wins, u = graphics.windows_for_clip(video, c["ranges"])
            c["graphic_windows"] = wins
            total += u.get("cost_usd", 0) or 0
        except Exception as e:
            sys.stderr.write(f"[graphics] {c.get('id')}: {e}\n")
            c["graphic_windows"] = []
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    _add_cost(job_dir, total)


def _pipeline(job_dir, job_id):
    try:
        if not os.path.isfile(os.path.join(job_dir, "transcript.json")):
            _write_status(job_dir, status="processing",
                          stage="Transcribing with ElevenLabs…")
            import transcribe
            transcribe.transcribe(_find_video(job_dir),
                                  os.path.join(job_dir, "transcript.json"))
        _write_status(job_dir, status="processing", stage="Reading transcript…")
        _run("prep", job_dir)
        _write_status(job_dir, status="processing", stage="Picking + scoring clips…")
        clips, usage = brain.pick_clips(job_dir)
        if not clips:
            raise RuntimeError("The AI didn't return any clips. Check the transcript.")
        _write_status(job_dir, cost=usage)
        if os.environ.get("GRAPHICS", "1") != "0":
            _write_status(job_dir, status="processing",
                          stage="Checking for on-screen graphics…")
            _annotate_graphics(job_dir)
        _write_status(job_dir, status="processing", stage=f"Cutting {len(clips)} clips…")
        _run("render", job_dir)
        _write_status(job_dir, status="processing", stage="Adding Hinglish captions…")
        _run("caption", job_dir)
        st = _read_status(job_dir)
        _write_status(job_dir, status="done", stage="", message="",
                      clips=st.get("clips", []))
    except Exception as e:
        _write_status(job_dir, status="error", stage="", message=str(e))
        sys.stderr.write(f"[pipeline {job_id}] {traceback.format_exc()}\n")


# ---------- shared client-side card renderer (used by both pages) ----------
CLIENT_JS = r"""
function scoreBadge(s){ if(s==null) return '';
  const col=s>=8?'#3ecf8e':(s>=5?'#e0a93e':'#e06a6a');
  return '<span class="badge" style="background:'+col+'">'+s+'/10</span>'; }
function esc(x){return (x==null?'':(''+x)).replace(/"/g,'&quot;');}
function cardHTML(c, jobId){
  const bust='?t='+Date.now();
  const sr=c.score_reason?('<p class="sr">'+c.score_reason+'</p>'):'';
  const hb=(c.hook_score!=null&&c.body_score!=null)?('<p class="sr">hook '+c.hook_score+'/10 &middot; body '+c.body_score+'/10</p>'):'';
  let opts='<option value="">rate</option>'; for(let i=10;i>=1;i--){opts+='<option value="'+i+'"'+(c.user_rating==i?' selected':'')+'>'+i+'</option>';}
  return '<div class="clip" id="clip-'+c.id+'">'
    +'<div class="scorerow"><h3>'+(c.hook||c.id)+'</h3>'+scoreBadge(c.score)+'</div>'
    +(c.why?'<p class="why">'+c.why+'</p>':'')+sr+hb
    +'<video src="'+c.url+bust+'" controls></video>'
    +'<a class="dl" href="'+c.url+bust+'" download>Download</a>'
    +'<div class="recut"><input id="ri-'+c.id+'" placeholder="change this clip: start 2s earlier, tighter, cut the intro..."><button onclick="recutClip(\''+jobId+'\',\''+c.id+'\')">Re-cut</button><span class="rmsg" id="rm-'+c.id+'"></span></div>'
    +'<div class="rate">Your rating: <select id="rs-'+c.id+'">'+opts+'</select>'
    +'<input id="rn-'+c.id+'" placeholder="why good/bad? (optional)" value="'+esc(c.user_note)+'">'
    +'<button onclick="rateClip(\''+jobId+'\',\''+c.id+'\')">Save</button><span class="saved" id="sv-'+c.id+'"></span></div>'
    +'</div>';
}
function renderCards(container, clips, jobId){
  container.innerHTML='';
  clips.forEach(c=>{ const d=document.createElement('div'); d.innerHTML=cardHTML(c,jobId); container.appendChild(d.firstChild); });
}
async function recutClip(jobId, clipId){
  const inp=document.getElementById('ri-'+clipId), msg=document.getElementById('rm-'+clipId);
  const instruction=(inp.value||'').trim(); if(!instruction){msg.textContent='type a change first';return;}
  msg.innerHTML='<span class="spin"></span>re-cutting (~1-2 min)…';
  try{
    const d=await (await fetch('/recut',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:jobId,clip_id:clipId,instruction})})).json();
    if(!d.ok){msg.textContent=d.error||'failed';return;}
    const card=document.getElementById('clip-'+clipId), tmp=document.createElement('div');
    tmp.innerHTML=cardHTML(d.clip,jobId); card.replaceWith(tmp.firstChild);
    if(typeof loadRecent==='function') loadRecent();
  }catch(e){msg.textContent=''+e;}
}
async function rateClip(jobId, clipId){
  const rv=document.getElementById('rs-'+clipId).value;
  const note=(document.getElementById('rn-'+clipId).value||'').trim();
  const sv=document.getElementById('sv-'+clipId);
  if(!rv){sv.textContent='pick a score';return;}
  sv.textContent='saving…';
  try{
    const d=await (await fetch('/rate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:jobId,clip_id:clipId,rating:parseInt(rv,10),note})})).json();
    sv.textContent=d.ok?'saved ✓ (it will learn from this)':'failed';
  }catch(e){sv.textContent=''+e;}
}
"""

STYLE = r"""
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family:-apple-system,Segoe UI,Roboto,sans-serif; max-width:680px;
    margin:40px auto; padding:0 20px; background:#0f0f11; color:#e8e8ea; }
  h1 { font-size:22px; margin:0 0 4px; }
  a.back { color:#6c6cf0; text-decoration:none; font-size:14px; }
  p.sub { color:#9a9aa2; margin:0 0 22px; font-size:14px; }
  .bar { display:flex; justify-content:space-between; align-items:center;
    background:#141418; border:1px solid #26262c; border-radius:10px;
    padding:10px 14px; margin-bottom:20px; font-size:13px; color:#b7b7bf; }
  .bar b { color:#e8e8ea; }
  .drop { display:block; border:1.5px dashed #3a3a40; border-radius:12px; padding:20px;
    margin-bottom:12px; cursor:pointer; transition:border-color .15s,background .15s; }
  .drop:hover,.drop.over { border-color:#6c6cf0; background:#17171b; }
  .drop .label { font-size:14px; font-weight:600; }
  .drop .hint { font-size:12px; color:#8a8a92; margin-top:4px; }
  .drop .file { font-size:13px; color:#7ee08a; margin-top:8px; word-break:break-all; }
  input[type=file] { display:none; }
  form>button { width:100%; padding:13px; border:0; border-radius:10px;
    background:#6c6cf0; color:#fff; font-size:15px; font-weight:600; cursor:pointer; margin-top:6px; }
  form>button:disabled { opacity:.5; cursor:not-allowed; }
  .status { margin-top:18px; font-size:14px; color:#b7b7bf; min-height:20px; }
  .err { color:#ff8a8a; white-space:pre-wrap; font-size:13px; }
  .results { margin-top:22px; }
  .clip { background:#17171b; border:1px solid #2a2a30; border-radius:12px; padding:14px; margin-bottom:16px; }
  .scorerow { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
  .clip h3 { margin:0 0 2px; font-size:15px; }
  .badge { color:#08130d; font-weight:700; font-size:12px; padding:2px 9px; border-radius:20px; white-space:nowrap; }
  .clip .why { margin:6px 0 4px; font-size:12px; color:#9a9aa2; }
  .sr { margin:0 0 8px; font-size:11px; color:#7a7a82; }
  video { width:100%; max-height:72vh; border-radius:8px; background:#000; display:block; }
  a.dl { display:inline-block; margin:10px 0 4px; color:#6c6cf0; text-decoration:none; font-size:14px; font-weight:600; }
  .recut { display:flex; gap:6px; margin-top:10px; align-items:center; flex-wrap:wrap; }
  .rate { display:flex; gap:6px; margin-top:8px; align-items:center; flex-wrap:wrap; font-size:12px; color:#9a9aa2; }
  .recut input,.rate input { flex:1; min-width:150px; padding:8px; border-radius:8px;
    border:1px solid #2a2a30; background:#0f0f11; color:#e8e8ea; font-size:12px; }
  .rate select { padding:6px; border-radius:8px; background:#0f0f11; color:#e8e8ea; border:1px solid #2a2a30; }
  .recut button,.rate button { width:auto; padding:8px 12px; border:0; border-radius:8px;
    background:#2f2f3a; color:#e8e8ea; font-size:12px; font-weight:600; cursor:pointer; }
  .rmsg,.saved { font-size:12px; color:#9a9aa2; }
  h2.rh { font-size:14px; margin:30px 0 10px; color:#9a9aa2; display:none; }
  .rj { display:block; background:#17171b; border:1px solid #2a2a30; border-radius:10px;
    padding:12px 14px; margin-bottom:8px; color:#e8e8ea; text-decoration:none; font-size:13px; }
  .spin { display:inline-block; width:14px; height:14px; border:2px solid #444;
    border-top-color:#6c6cf0; border-radius:50%; animation:s .8s linear infinite; vertical-align:-2px; margin-right:8px; }
  @keyframes s { to { transform:rotate(360deg); } }
"""

PAGE = ("""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clip Editor</title><style>""" + STYLE + """</style>
<h1>Clip Editor</h1>
<p class="sub">Drop a long video. It transcribes, finds, scores, cuts and captions the best clips. Rate them and it learns.</p>
<div class="bar"><a href="/learning" style="color:#6c6cf0;text-decoration:none">&#129504; What it&rsquo;s learned</a><span>Session spend <b id="total">$0.00</b></span></div>
<form id="form">
  <label class="drop" id="d-video"><div class="label">1 &middot; Long-form video</div>
    <div class="hint">MP4 / MOV &mdash; click or drag here</div><div class="file" id="f-video"></div>
    <input type="file" id="video" accept="video/*,.mov,.mp4,.mkv,.webm"></label>
  <label class="drop" id="d-tr"><div class="label">2 &middot; Transcript JSON <span style="color:#8a8a92;font-weight:400">(optional)</span></div>
    <div class="hint">leave empty &mdash; it&rsquo;ll auto-transcribe with ElevenLabs</div><div class="file" id="f-tr"></div>
    <input type="file" id="transcript" accept=".json,application/json"></label>
  <button type="submit" id="go" disabled>Make clips</button>
</form>
<div class="status" id="status"></div>
<div class="results" id="results"></div>
<h2 class="rh" id="recent-h">Recent</h2><div id="recent"></div>
<script>""" + CLIENT_JS + r"""
const vIn=document.getElementById('video'),tIn=document.getElementById('transcript');
const go=document.getElementById('go'),statusEl=document.getElementById('status');
const results=document.getElementById('results'),totalEl=document.getElementById('total');
let poll=null;
function wire(inputEl,dropId,fileId){
  const drop=document.getElementById(dropId),show=document.getElementById(fileId);
  inputEl.addEventListener('change',()=>{show.textContent=inputEl.files[0]?inputEl.files[0].name:'';refresh();});
  ['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('over');}));
  ['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('over');}));
  drop.addEventListener('drop',ev=>{if(ev.dataTransfer.files[0]){inputEl.files=ev.dataTransfer.files;show.textContent=inputEl.files[0].name;refresh();}});
}
function refresh(){go.disabled=!vIn.files[0];}
wire(vIn,'d-video','f-video');wire(tIn,'d-tr','f-tr');
async function loadRecent(){
  try{
    const jobs=await (await fetch('/recent')).json();
    let total=0; jobs.forEach(j=>{if(j.cost&&j.cost.cost_usd)total+=j.cost.cost_usd;});
    totalEl.textContent='$'+total.toFixed(2);
    const box=document.getElementById('recent'),h=document.getElementById('recent-h');
    const done=jobs.filter(j=>j.status==='done'&&j.nclips>0);
    h.style.display=done.length?'block':'none'; box.innerHTML='';
    for(const j of done){const a=document.createElement('a');a.href='/job/'+j.id;a.className='rj';
      const c=j.cost&&j.cost.cost_usd?(' &middot; $'+j.cost.cost_usd.toFixed(3)):'';
      a.innerHTML='<b>'+j.nclips+' clip'+(j.nclips>1?'s':'')+'</b>'+c+' &middot; <span style="color:#8a8a92">'+(j.video||j.id)+'</span>';
      box.appendChild(a);}
  }catch(e){}
}
loadRecent();
document.getElementById('form').addEventListener('submit',async(e)=>{
  e.preventDefault();go.disabled=true;results.innerHTML='';
  statusEl.innerHTML='<span class="spin"></span>Uploading…';
  const fd=new FormData();fd.append('video',vIn.files[0]);fd.append('transcript',tIn.files[0]);
  try{
    const d=await (await fetch('/submit',{method:'POST',body:fd})).json();
    if(!d.ok){statusEl.innerHTML='<span class="err">'+d.error+'</span>';go.disabled=false;return;}
    startPolling(d.job_id);
  }catch(err){statusEl.innerHTML='<span class="err">'+err+'</span>';go.disabled=false;}
});
function startPolling(jobId){
  if(poll)clearInterval(poll);
  const tick=async()=>{
    const d=await (await fetch('/status/'+jobId)).json();
    if(d.status==='done'){clearInterval(poll);poll=null;
      const cost=d.cost&&d.cost.cost_usd?(' &middot; cost $'+d.cost.cost_usd.toFixed(3)):'';
      statusEl.innerHTML='Done — '+d.clips.length+' clip(s)'+cost+'.';
      renderCards(results,d.clips,jobId);go.disabled=false;loadRecent();
    }else if(d.status==='error'){clearInterval(poll);poll=null;
      statusEl.innerHTML='<span class="err">'+(d.message||'Something went wrong')+'</span>';go.disabled=false;
    }else{statusEl.innerHTML='<span class="spin"></span>'+(d.stage||'Working…');}
  };
  tick();poll=setInterval(tick,2000);
}
</script>""")

JOB_PAGE = ("""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clips</title><style>""" + STYLE + """</style>
<a class="back" href="/">&larr; New clip</a>
<h1 id="hd">Clips</h1><p class="sub" id="vid"></p>
<div class="results" id="results"></div>
<script>""" + CLIENT_JS + r"""
const JOBID="__JOBID__";
function loadRecent(){} // no-op on this page
(async()=>{
  const d=await (await fetch('/status/'+JOBID)).json();
  const cost=d.cost&&d.cost.cost_usd?(' · $'+d.cost.cost_usd.toFixed(3)):'';
  document.getElementById('hd').textContent=(d.clips?d.clips.length:0)+' clip(s)'+cost;
  document.getElementById('vid').textContent=d.video||'';
  renderCards(document.getElementById('results'),d.clips||[],JOBID);
})();
</script>""")


@app.route("/")
def index():
    return PAGE


@app.route("/submit", methods=["POST"])
def submit():
    video = request.files.get("video")
    if not video or not video.filename:
        return {"ok": False, "error": "No video file."}
    tr = request.files.get("transcript")  # optional — auto-transcribe if absent
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    video.save(os.path.join(job_dir, os.path.basename(video.filename)))
    if tr and tr.filename:
        tr.save(os.path.join(job_dir, "transcript.json"))
    _write_status(job_dir, status="processing", stage="Queued…", message="",
                  video=os.path.basename(video.filename), clips=[], cost=None)
    threading.Thread(target=_pipeline, args=(job_dir, job_id), daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.route("/status/<job_id>")
def status(job_id):
    job_dir = os.path.join(JOBS_DIR, job_id)
    if not os.path.isdir(job_dir):
        return {"status": "error", "message": "Unknown job.", "clips": []}
    return _read_status(job_dir)


@app.route("/recent")
def recent():
    jobs = []
    for name in sorted(os.listdir(JOBS_DIR), reverse=True):
        d = os.path.join(JOBS_DIR, name)
        if not os.path.isdir(d):
            continue
        st = _read_status(d)
        jobs.append({"id": name, "status": st.get("status"),
                     "nclips": len(st.get("clips", [])),
                     "video": st.get("video", ""), "cost": st.get("cost")})
    return jsonify(jobs[:40])


@app.route("/recut", methods=["POST"])
def recut():
    d = request.get_json(force=True, silent=True) or {}
    job_id, clip_id = d.get("job_id"), d.get("clip_id")
    instruction = (d.get("instruction") or "").strip()
    job_dir = os.path.join(JOBS_DIR, job_id or "")
    if not os.path.isdir(job_dir):
        return {"ok": False, "error": "unknown job"}
    if not instruction:
        return {"ok": False, "error": "no instruction"}
    try:
        clip, usage = brain.recut_clip(job_dir, clip_id, instruction)
        if os.environ.get("GRAPHICS", "1") != "0":
            _annotate_graphics(job_dir, only=clip_id)
        _run("render", job_dir, clip_id)
        _run("caption", job_dir, clip_id)
        fname = f"{brain.slug(clip_id)}.mp4"
        meta = {"id": clip_id, "hook": clip.get("hook", ""), "why": clip.get("why", ""),
                "hook_score": clip.get("hook_score"), "body_score": clip.get("body_score"),
                "score": clip.get("score"), "score_reason": clip.get("score_reason", ""),
                "url": f"/jobs/{job_id}/{fname}"}
        st = _read_status(job_dir)
        clips = st.get("clips", [])
        for i, c in enumerate(clips):
            if c.get("id") == clip_id:
                meta["user_rating"] = c.get("user_rating")
                meta["user_note"] = c.get("user_note")
                clips[i] = meta
                break
        else:
            clips.append(meta)
        cost = dict(st.get("cost") or {})
        cost["cost_usd"] = round((cost.get("cost_usd") or 0) + usage["cost_usd"], 4)
        _write_status(job_dir, clips=clips, cost=cost)
        return {"ok": True, "clip": meta}
    except Exception as e:
        sys.stderr.write("[recut] " + traceback.format_exc() + "\n")
        return {"ok": False, "error": str(e)}


@app.route("/rate", methods=["POST"])
def rate():
    d = request.get_json(force=True, silent=True) or {}
    job_id, clip_id = d.get("job_id"), d.get("clip_id")
    rating, note = d.get("rating"), (d.get("note") or "").strip()
    job_dir = os.path.join(JOBS_DIR, job_id or "")
    if not os.path.isdir(job_dir):
        return {"ok": False, "error": "unknown job"}
    st = _read_status(job_dir)
    clip = next((c for c in st.get("clips", []) if c.get("id") == clip_id), {})
    brain.save_feedback({
        "job": job_id, "clip_id": clip_id, "hook": clip.get("hook", ""),
        "auto_score": clip.get("score"), "rating": rating, "note": note,
        "video": st.get("video", ""), "ts": round(time.time()),
    })
    for c in st.get("clips", []):
        if c.get("id") == clip_id:
            c["user_rating"] = rating
            c["user_note"] = note
    _write_status(job_dir, clips=st.get("clips", []))
    return {"ok": True}


LEARN_PAGE = ("""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>What it's learned</title><style>""" + STYLE + r"""
  .lbox { background:#141418; border:1px solid #26262c; border-radius:12px; padding:16px; margin:0 0 20px; }
  .lbox pre { white-space:pre-wrap; font:inherit; font-size:13px; color:#d7d7de; margin:10px 0 0; }
  .grp { font-size:13px; color:#9a9aa2; margin:22px 0 8px; }
  .fb { background:#17171b; border:1px solid #2a2a30; border-radius:10px; padding:10px 12px; margin-bottom:8px; }
  .fb .h { font-size:13px; }
  .fb .n { font-size:12px; color:#9a9aa2; margin-top:3px; }
  .pill { font-weight:700; font-size:12px; padding:2px 8px; border-radius:20px; color:#08130d; margin-right:8px; }
</style>
<a class="back" href="/">&larr; Back</a>
<h1>&#129504; What it&rsquo;s learned</h1>
<p class="sub">Everything the editor has picked up from your ratings. The more you rate, the sharper this gets.</p>
<div class="bar" id="stats"><span>No ratings yet.</span></div>
<div class="lbox">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <b>Your taste, in its own words</b>
    <button id="gen" onclick="genSummary()" style="width:auto;padding:8px 12px;border:0;border-radius:8px;background:#2f2f3a;color:#e8e8ea;font-size:12px;font-weight:600;cursor:pointer">Refresh</button>
  </div>
  <pre id="summary">Loading…</pre>
</div>
<div id="groups"></div>
<script>
function pill(s){const c=s>=8?'#3ecf8e':(s>=5?'#e0a93e':'#e06a6a');return '<span class="pill" style="background:'+c+'">'+s+'/10</span>';}
function group(title, rows){
  if(!rows.length) return '';
  let h='<div class="grp">'+title+' ('+rows.length+')</div>';
  rows.forEach(r=>{h+='<div class="fb"><div class="h">'+pill(r.rating)+(r.hook||'(clip)')+'</div>'+(r.note?'<div class="n">&ldquo;'+r.note+'&rdquo;</div>':'')+'</div>';});
  return h;
}
async function load(){
  const d=await (await fetch('/learning/data')).json();
  const s=d.stats;
  document.getElementById('stats').innerHTML = s.count?('<span>'+s.count+' clips rated</span><span>avg <b>'+s.avg+'/10</b></span>'):'<span>No ratings yet — rate some clips first.</span>';
  document.getElementById('summary').textContent = d.summary || 'No summary yet — hit Refresh once you\'ve rated a few clips.';
  const rows=d.rows||[];
  document.getElementById('groups').innerHTML =
     group('Loved it', rows.filter(r=>r.rating>=8))
   + group('Just OK', rows.filter(r=>r.rating>=5&&r.rating<8))
   + group('Cut it', rows.filter(r=>r.rating<5));
}
async function genSummary(){
  const b=document.getElementById('gen'); b.textContent='thinking…'; b.disabled=true;
  try{ const d=await (await fetch('/learning/summary',{method:'POST'})).json();
    document.getElementById('summary').textContent=d.summary||d.error||'(nothing yet)';
  }catch(e){document.getElementById('summary').textContent=''+e;}
  b.textContent='Refresh'; b.disabled=false;
}
load();
</script>""")


@app.route("/learning")
def learning():
    return LEARN_PAGE


@app.route("/learning/data")
def learning_data():
    rows = brain.load_feedback()
    rated = [r for r in rows if r.get("rating") is not None]
    count = len(rated)
    avg = round(sum(r["rating"] for r in rated) / count, 1) if count else 0
    return jsonify({
        "rows": [{"rating": r.get("rating"), "hook": r.get("hook", ""),
                  "note": r.get("note", ""), "video": r.get("video", "")}
                 for r in reversed(rated)],
        "stats": {"count": count, "avg": avg},
        "summary": brain.cached_summary(),
    })


@app.route("/learning/summary", methods=["POST"])
def learning_summary_route():
    try:
        text, _ = brain.learning_summary()
        return {"ok": True, "summary": text}
    except Exception as e:
        return {"ok": False, "summary": "", "error": str(e)}


@app.route("/job/<job_id>")
def job_view(job_id):
    if not os.path.isdir(os.path.join(JOBS_DIR, job_id)):
        return "Unknown job", 404
    return JOB_PAGE.replace("__JOBID__", job_id)


@app.route("/jobs/<job>/<name>")
def job_file(job, name):
    return send_from_directory(os.path.join(JOBS_DIR, job), name)


if __name__ == "__main__":
    print("\n  Clip Editor  —  http://127.0.0.1:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
