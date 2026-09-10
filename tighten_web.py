#!/usr/bin/env python3
"""
Web front-end for the tightening engine, mounted at /tighten.

Kept as a Blueprint so the reels flow in app.py is untouched: same app, same
link, same password, but a change here cannot break clip-making.

Pipeline: upload -> ElevenLabs transcript (if none supplied) -> tighten.py ->
review.py -> cleaned media + conform log + a list of spans worth checking.
"""

import json
import os
import re
import threading
import time
import traceback
import uuid

from urllib.parse import quote

from flask import Blueprint, jsonify, request

import autofix
import editlog
import review as review_mod
import tighten as tighten_mod

bp = Blueprint("tighten_web", __name__)

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
AUDIO_EXTS = (".wav", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus")
MEDIA_EXTS = VIDEO_EXTS + AUDIO_EXTS

_JOBS_DIR = None
_STYLE = ""


def init(jobs_dir, style):
    global _JOBS_DIR, _STYLE
    _JOBS_DIR = jobs_dir
    _STYLE = style


# ---------- status helpers (same status.json the reels flow polls) ----------

def _status_path(d):
    return os.path.join(d, "status.json")


def _read_status(job_dir):
    try:
        with open(_status_path(job_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"status": "unknown", "stage": "", "message": ""}


def _write_status(job_dir, **kw):
    st = _read_status(job_dir)
    st.update(kw)
    with open(_status_path(job_dir), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _safe_name(name):
    """Filesystem/URL-safe version of an uploaded filename, extension intact.

    A browser download of "Yt2.m4a" comes back as "Yt2.m4a (1).mp4"; those spaces
    and parens land in /jobs/<id>/<file> and the download link fails.
    """
    base = os.path.basename(name or "upload")
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "upload"
    return stem[:80] + ext.lower()


def _merge_cost(*usages):
    """Sum the model spend from every call this job made."""
    total, model = 0.0, None
    for u in usages:
        if not u:
            continue
        total += u.get("cost_usd") or 0
        model = model or u.get("model")
    if model is None:
        return None
    return {"model": model, "cost_usd": round(total, 4)}


def _find_media(job_dir):
    for n in sorted(os.listdir(job_dir)):
        if os.path.splitext(n)[1].lower() in MEDIA_EXTS:
            return os.path.join(job_dir, n)
    return None


# ---------- pipeline ----------

def _pipeline(job_dir, job_id):
    try:
        media = _find_media(job_dir)
        if not media:
            raise RuntimeError("No media file found in the job.")
        stem = os.path.splitext(os.path.basename(media))[0]
        transcript = os.path.join(job_dir, "transcript.json")

        if not os.path.isfile(transcript):
            _write_status(job_dir, status="processing",
                          stage="Transcribing with ElevenLabs...")
            import transcribe
            transcribe.transcribe(media, transcript)

        _, has_video = tighten_mod.probe(media)
        out_name = stem + "_TIGHT" + (".mp4" if has_video else ".wav")
        out_path = os.path.join(job_dir, out_name)

        _write_status(job_dir, status="processing",
                      stage="Cutting dead air and retakes...")
        stats = tighten_mod.tighten(
            media, transcript, out_path, workdir=job_dir,
            progress=lambda m: _write_status(job_dir, stage=m))

        flags, usage = [], None
        if os.environ.get("REVIEW", "1") != "0":
            _write_status(job_dir, status="processing",
                          stage="Reviewing the cut for broken sentences...")
            try:
                flags, usage = review_mod.review(transcript, stats["keeps"])
            except Exception as e:
                # a failed review must never lose the user their cut
                _write_status(job_dir, review_error=str(e))

        # ---- auto-fix: turn the flags into overrides and re-cut ----
        # Only runs when review found something. Every proposed window is
        # validated before use, and the re-cut is kept only if it measurably
        # beats the original - otherwise the first cut stands.
        applied, rejected, fix_usage = [], [], None
        if flags and os.environ.get("AUTOFIX", "1") != "0":
            try:
                _write_status(job_dir, stage="Working out how to fix %d issue(s)..."
                              % len(flags))
                proposed, fix_usage = autofix.propose(transcript, flags)
                words0 = tighten_mod.load_words(transcript)
                deleted0, _ = tighten_mod.detect_repeats(words0)
                sem0 = tighten_mod.keep_intervals(words0, deleted0)
                applied, rejected = autofix.validate(
                    proposed, sem0, stats["source_sec"], words0)

                if applied:
                    _write_status(job_dir, stage="Re-cutting with %d fix(es)..."
                                  % len(applied))
                    fixed_path = out_path + ".fix" + os.path.splitext(out_path)[1]
                    stats2 = tighten_mod.tighten(
                        media, transcript, fixed_path, overrides=applied,
                        workdir=job_dir,
                        progress=lambda m: _write_status(job_dir, stage=m))
                    flags2, u3 = review_mod.review(transcript, stats2["keeps"])
                    if u3 and fix_usage:
                        fix_usage = dict(
                            fix_usage,
                            cost_usd=round((fix_usage.get("cost_usd") or 0)
                                           + (u3.get("cost_usd") or 0), 4))
                    if autofix.better(flags2, flags):
                        os.replace(fixed_path, out_path)
                        stats, flags = stats2, flags2
                    else:
                        # the re-cut did not help; keep the original
                        applied = []
                        try:
                            os.remove(fixed_path)
                        except OSError:
                            pass
            except Exception as e:
                # a failed repair must never cost the user their cut
                _write_status(job_dir, autofix_error=str(e))
                applied = []

        words = tighten_mod.load_words(transcript)
        lines = tighten_mod.cut_lines(words, stats["keeps"])
        log_name = stem + "_TIGHT_editlog.txt"
        editlog.write_editlog(os.path.join(job_dir, log_name),
                              os.path.basename(media), stats, lines, flags,
                              fixes=applied)

        stats.pop("keeps", None)  # too big for the status payload
        _write_status(
            job_dir, status="done", stage="", message="",
            mode="tighten", tighten=stats, flags=flags,
            fixes_applied=applied, fixes_rejected=rejected,
            cost=_merge_cost(usage, fix_usage),
            outputs=[
                {"name": out_name,
                 "url": "/jobs/%s/%s" % (job_id, quote(out_name)),
                 "label": "Cleaned " + ("video" if has_video else "audio")},
                {"name": log_name,
                 "url": "/jobs/%s/%s" % (job_id, quote(log_name)),
                 "label": "Conform log"},
            ])
    except Exception as e:
        _write_status(job_dir, status="error", stage="", message=str(e))
        import sys
        sys.stderr.write("[tighten %s] %s\n" % (job_id, traceback.format_exc()))


# ---------- routes ----------

@bp.route("/tighten/submit", methods=["POST"])
def submit():
    media = request.files.get("media")
    if not media or not media.filename:
        return {"ok": False, "error": "No file."}
    if os.path.splitext(media.filename)[1].lower() not in MEDIA_EXTS:
        return {"ok": False, "error": "Unsupported file type."}
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    job_dir = os.path.join(_JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    fname = _safe_name(media.filename)
    media.save(os.path.join(job_dir, fname))
    tr = request.files.get("transcript")
    if tr and tr.filename:
        tr.save(os.path.join(job_dir, "transcript.json"))
    _write_status(job_dir, status="processing", stage="Queued...", message="",
                  mode="tighten", video=fname)
    threading.Thread(target=_pipeline, args=(job_dir, job_id), daemon=True).start()
    return {"ok": True, "job_id": job_id}


@bp.route("/tighten/recent")
def recent():
    jobs = []
    for name in sorted(os.listdir(_JOBS_DIR), reverse=True):
        d = os.path.join(_JOBS_DIR, name)
        if not os.path.isdir(d):
            continue
        st = _read_status(d)
        if st.get("mode") != "tighten" or st.get("status") != "done":
            continue
        t = st.get("tighten") or {}
        jobs.append({"id": name, "video": st.get("video", ""),
                     "source_sec": t.get("source_sec"),
                     "output_sec": t.get("output_sec"),
                     "nflags": len(st.get("flags") or []),
                     "outputs": st.get("outputs") or []})
    return jsonify(jobs[:25])


PAGE_TMPL = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tighten a VO</title><style>__STYLE__
  .modes{display:flex;gap:8px;margin-bottom:18px}
  .modes a{flex:1;text-align:center;padding:9px;border-radius:9px;font-size:13px;
    text-decoration:none;border:1px solid #26262c;color:#9a9aa2;background:#141418}
  .modes a.on{background:#6c6cf0;border-color:#6c6cf0;color:#fff;font-weight:600}
  .stat{display:flex;gap:14px;flex-wrap:wrap;background:#141418;border:1px solid #26262c;
    border-radius:10px;padding:12px 14px;margin:14px 0;font-size:13px;color:#b7b7bf}
  .stat b{color:#e8e8ea;display:block;font-size:16px}
  .dl{display:inline-block;margin:6px 8px 0 0;padding:9px 14px;border-radius:8px;
    background:#6c6cf0;color:#fff;text-decoration:none;font-size:13px;font-weight:600}
  .dl.alt{background:#26262c;color:#e8e8ea}
  .flag{border:1px solid #26262c;border-left:3px solid #e0a93e;border-radius:8px;
    padding:10px 12px;margin:8px 0;background:#141418;font-size:13px}
  .flag.high{border-left-color:#e06a6a}
  .flag.fixed{border-left-color:#3ecf8e}
  .flag .t{color:#8a8a92;font-size:12px}
  .flag .q{color:#e8e8ea;margin:4px 0}
  .flag .i{color:#b7b7bf}
  .flag .f{color:#3ecf8e;margin-top:3px}
  .ok{color:#3ecf8e;font-size:13px}
</style>
<h1>Tighten a VO</h1>
<p class="sub">Drop a voiceover. It removes dead air and repeated takes, then Claude
checks the result for sentences the cut may have broken.</p>
<div class="modes"><a href="/">Make clips</a><a href="/tighten" class="on">Tighten a VO</a></div>
<form id="form">
  <label class="drop" id="d-media"><div class="label">1 &middot; Video or audio</div>
    <div class="hint">MP4 / MOV / WAV / M4A / MP3 &mdash; click or drag here</div>
    <div class="file" id="f-media"></div>
    <input type="file" id="media" accept="video/*,audio/*"></label>
  <label class="drop" id="d-tr"><div class="label">2 &middot; Transcript JSON
    <span style="color:#8a8a92;font-weight:400">(optional)</span></div>
    <div class="hint">leave empty &mdash; it&rsquo;ll auto-transcribe with ElevenLabs</div>
    <div class="file" id="f-tr"></div>
    <input type="file" id="transcript" accept=".json,application/json"></label>
  <button type="submit" id="go" disabled>Tighten it</button>
</form>
<div class="status" id="status"></div>
<div id="results"></div>
<h2 class="rh" id="recent-h" style="display:none">Recent</h2><div id="recent"></div>
<script>
const mIn=document.getElementById('media'),tIn=document.getElementById('transcript');
const go=document.getElementById('go'),statusEl=document.getElementById('status');
const results=document.getElementById('results');
let poll=null;
function wire(inputEl,dropId,fileId){
  const drop=document.getElementById(dropId),show=document.getElementById(fileId);
  inputEl.addEventListener('change',()=>{show.textContent=inputEl.files[0]?inputEl.files[0].name:'';refresh();});
  ['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('over');}));
  ['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('over');}));
  drop.addEventListener('drop',ev=>{if(ev.dataTransfer.files[0]){inputEl.files=ev.dataTransfer.files;show.textContent=inputEl.files[0].name;refresh();}});
}
function refresh(){go.disabled=!mIn.files[0];}
wire(mIn,'d-media','f-media');wire(tIn,'d-tr','f-tr');
function mmss(s){const m=Math.floor(s/60);return m+'m'+String(Math.round(s%60)).padStart(2,'0')+'s';}
function esc(x){return (x==null?'':(''+x)).replace(/&/g,'&amp;').replace(/</g,'&lt;');}
function renderDone(d,jobId){
  const t=d.tighten||{},fl=d.flags||[];
  let h='<div class="stat">'
    +'<div>Source<b>'+mmss(t.source_sec||0)+'</b></div>'
    +'<div>Output<b>'+mmss(t.output_sec||0)+'</b></div>'
    +'<div>Kept<b>'+(t.kept_pct||0)+'%</b></div>'
    +'<div>Retake cuts<b>'+(t.retake_cuts||0)+'</b></div>'
    +'<div>Speech kept<b>'+(t.speech_kept_pct||0)+'%</b></div></div>';
  (d.outputs||[]).forEach((o,i)=>{h+='<a class="dl'+(i?' alt':'')+'" href="'+o.url+'" download>'+esc(o.label)+'</a>';});
  const fx=d.fixes_applied||[];
  if(fx.length){
    h+='<h2 class="rh">'+fx.length+' issue'+(fx.length>1?'s':'')+' repaired automatically</h2>';
    fx.forEach(f=>{h+='<div class="flag fixed">'
      +'<div class="t">FIXED &middot; at '+f.win[0].toFixed(2)+'s</div>'
      +'<div class="i">'+esc(f.why||'')+'</div></div>';});
  }
  if(fl.length){
    h+='<h2 class="rh">'+fl.length+' span'+(fl.length>1?'s':'')+' still worth checking</h2>';
    fl.forEach(f=>{h+='<div class="flag '+(f.severity==='high'?'high':'')+'">'
      +'<div class="t">'+f.severity.toUpperCase()+' &middot; line '+f.line+' &middot; '+f.start.toFixed(2)+'s</div>'
      +'<div class="q">'+esc(f.text||'(no words)')+'</div>'
      +'<div class="i">'+esc(f.issue)+'</div>'
      +(f.fix?'<div class="f">Fix: '+esc(f.fix)+'</div>':'')+'</div>';});
  } else { h+='<p class="ok">'+(fx.length?'Nothing else':'Review pass found nothing')
             +' broken.</p>'; }
  results.innerHTML=h;
}
async function loadRecent(){
  try{
    const jobs=await (await fetch('/tighten/recent')).json();
    const box=document.getElementById('recent'),hh=document.getElementById('recent-h');
    hh.style.display=jobs.length?'block':'none';box.innerHTML='';
    jobs.forEach(j=>{const a=document.createElement('a');
      a.href=(j.outputs[0]||{}).url||'#';a.className='rj';a.setAttribute('download','');
      a.innerHTML='<b>'+mmss(j.source_sec||0)+' &rarr; '+mmss(j.output_sec||0)+'</b>'
        +(j.nflags?(' &middot; '+j.nflags+' to check'):'')
        +' &middot; <span style="color:#8a8a92">'+esc(j.video||j.id)+'</span>';
      box.appendChild(a);});
  }catch(e){}
}
loadRecent();
document.getElementById('form').addEventListener('submit',async(e)=>{
  e.preventDefault();go.disabled=true;results.innerHTML='';
  statusEl.innerHTML='<span class="spin"></span>Uploading...';
  const fd=new FormData();fd.append('media',mIn.files[0]);
  if(tIn.files[0])fd.append('transcript',tIn.files[0]);
  try{
    const d=await (await fetch('/tighten/submit',{method:'POST',body:fd})).json();
    if(!d.ok){statusEl.innerHTML='<span class="err">'+esc(d.error)+'</span>';go.disabled=false;return;}
    startPolling(d.job_id);
  }catch(err){statusEl.innerHTML='<span class="err">'+err+'</span>';go.disabled=false;}
});
function startPolling(jobId){
  if(poll)clearInterval(poll);
  const tick=async()=>{
    const d=await (await fetch('/status/'+jobId)).json();
    if(d.status==='done'){clearInterval(poll);poll=null;
      const c=d.cost&&d.cost.cost_usd?(' &middot; review $'+d.cost.cost_usd.toFixed(3)):'';
      statusEl.innerHTML='Done'+c+'.';renderDone(d,jobId);go.disabled=false;loadRecent();
    }else if(d.status==='error'){clearInterval(poll);poll=null;
      statusEl.innerHTML='<span class="err">'+esc(d.message||'Something went wrong')+'</span>';go.disabled=false;
    }else{statusEl.innerHTML='<span class="spin"></span>'+esc(d.stage||'Working...');}
  };
  tick();poll=setInterval(tick,2000);
}
</script>"""


@bp.route("/tighten")
def page():
    return PAGE_TMPL.replace("__STYLE__", _STYLE)
