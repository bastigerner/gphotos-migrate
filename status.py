"""Kleine Statusseite für den Fotoumzug. Start: python3 -I status.py  ->  http://127.0.0.1:8899

Liest nur: state/library.json, state/done.jsonl, das neueste state/*.log, ~/Pictures/gphotos-work
und die Netzwerkzähler des Macs (netstat). Ändert nichts und spricht nicht mit Google.
"""
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
WORK = Path.home() / "Pictures" / "gphotos-work"
PORT = 8899

lib = {"mtime": 0, "items": {}}
samples = []  # (zeit, erledigte Fotos) für das Tempo
net = {"t": 0, "rx": 0, "tx": 0, "down": 0.0, "up": 0.0}


lib_lock = threading.Lock()


def library():
    # Die Datei ist mehrere hundert MB groß: nie von mehreren Anfragen gleichzeitig einlesen.
    with lib_lock:
        return _library()


def _library():
    f = STATE / "library.json"
    if f.exists() and f.stat().st_mtime != lib["mtime"]:
        lib["items"] = {
            p["uuid"]: {
                "size": p.get("original_filesize") or 0,
                "kind": "hidden" if p.get("hidden") else "favorite" if p.get("favorite") else "video" if p.get("ismovie") else "photo",
            }
            for p in json.loads(f.read_text())
            if not p.get("shared") and not p.get("intrash") and not (p.get("syndicated") and not p.get("saved_to_library"))
        }
        lib["mtime"] = f.stat().st_mtime
    return lib["items"]


def done_set():
    latest = {}
    f = STATE / "done.jsonl"
    if f.exists():
        for line in f.read_text().splitlines():
            r = json.loads(line)
            latest[r["uuid"]] = bool(r.get("id"))
    done = {u for u, ok in latest.items() if ok and not u.endswith("-original") and latest.get(u + "-original", True)}
    return done, sum(latest.values())


def network():
    """Gesamter Mac, nicht nur der Umzug: Summe aller physischen Schnittstellen laut netstat."""
    out = subprocess.run(["netstat", "-ibn"], capture_output=True, text=True).stdout.splitlines()
    rx = tx = 0
    seen = set()
    for line in out[1:]:
        c = line.split()
        if len(c) >= 10 and c[0].startswith("en") and c[0] not in seen and c[2].startswith("<Link"):
            seen.add(c[0])
            rx += int(c[6]); tx += int(c[9])
    now = time.time()
    if net["t"] and now > net["t"]:
        dt = now - net["t"]
        net["down"], net["up"] = max(rx - net["rx"], 0) / dt, max(tx - net["tx"], 0) / dt
    net.update(t=now, rx=rx, tx=tx)
    return net["down"], net["up"]


def batches():
    rows = []
    if WORK.exists():
        for d in sorted(WORK.glob("b*")):
            q = d / "query.json"
            total = len(json.loads(q.read_text())) if q.exists() else 0
            export, out = d / "export", d / "out"
            if (out / "results.json").exists() or (out / "manifest.json").exists() and not export.exists():
                m = len(json.loads((out / "manifest.json").read_text()))
                r = json.loads((out / "results.json").read_text()) if (out / "results.json").exists() else []
                up = sum(1 for x in r if x.get("id"))
                # Nach einem Google-Fehler gleicht reconcile.mjs minutenlang ab; ohne eigenen Zustand wirkt der Lauf eingefroren.
                checking = not rows and subprocess.run(["pgrep", "-f", "reconcile.mjs"], capture_output=True).returncode == 0
                rows.append({"name": d.name, "stage": "Abgleich mit Google nach Fehler (einige Minuten)" if checking else "lädt hoch" if up or not rows else "wartet auf Upload", "done": up, "total": m})
            elif out.exists():
                rows.append({"name": d.name, "stage": "wird aufbereitet", "done": 0, "total": total})
            else:
                # geladen wird in part0..partN, danach wandert alles nach export
                # Einträge zählen, nicht Dateien: Live-Fotos und bearbeitete Bilder bestehen aus mehreren Dateien je Eintrag
                n = len({f.name[:36] for sub in [export, *d.glob("part*")] if sub.exists() for f in sub.glob("*") if not f.name.startswith(("fetch", "."))})
                rows.append({"name": d.name, "stage": "lädt aus iCloud", "done": n, "total": total, "unit": "Einträge"})
    return rows


def status():
    items = library()
    done, files = done_set()
    cats = {k: {"total": 0, "done": 0, "bytes": 0, "bytes_done": 0} for k in ("photo", "video", "favorite", "hidden")}
    for u, p in items.items():
        c = cats[p["kind"]]
        c["total"] += 1; c["bytes"] += p["size"]
        if u in done:
            c["done"] += 1; c["bytes_done"] += p["size"]
    n_done = sum(c["done"] for c in cats.values())
    now = time.time()
    if not samples or samples[-1][1] != n_done or now - samples[-1][0] > 60:
        samples.append((now, n_done))
    recent = [s for s in samples if now - s[0] <= 1800]
    rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0]) * 3600 if len(recent) > 1 and recent[-1][0] > recent[0][0] else None
    logs = sorted(STATE.glob("*.log"), key=lambda f: f.stat().st_mtime)
    down, up = network()
    return {
        "running": subprocess.run(["pgrep", "-f", "run.py"], capture_output=True).returncode == 0,
        "cats": cats, "files": files, "rate_per_hour": rate, "down": down, "up": up,
        "batches": batches(), "log": logs[-1].read_text().splitlines()[-12:] if logs else [],
        "log_name": logs[-1].name if logs else "", "time": time.strftime("%H:%M:%S"),
    }


PAGE = """<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fotoumzug</title>
<style>
:root{--bg:#f6f6f4;--fg:#1c1c1a;--mut:#6b6b66;--line:#d9d9d4;--bar:#2f5d50;--track:#e6e6e1;--warn:#9a3b1f}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecece8;--mut:#9a9a93;--line:#30302d;--bar:#7fb5a3;--track:#262624;--warn:#e08a6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 40px}
h1{font-size:20px;margin:0 0 2px;font-weight:650}h2{font-size:13px;font-weight:600;color:var(--mut);margin:26px 0 8px;text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--mut);font-size:13px}.big{font-size:34px;font-weight:650;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.row{display:grid;grid-template-columns:130px 1fr 150px;gap:12px;align-items:center;padding:7px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.num{text-align:right;font-variant-numeric:tabular-nums;color:var(--mut);font-size:13px}
.track{height:8px;background:var(--track);border-radius:4px;overflow:hidden}.fill{height:100%;background:var(--bar);width:0}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}.kv b{display:block;font-size:20px;font-variant-numeric:tabular-nums;font-weight:600}
.kv span{color:var(--mut);font-size:13px}pre{margin:0;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word;color:var(--mut)}
.state{font-weight:600}.stop{color:var(--warn)}
@media(max-width:520px){.row{grid-template-columns:96px 1fr;}.num{grid-column:1/-1;text-align:left}}
</style><main>
<h1>Fotoumzug iCloud → Google Photos</h1><div class="sub"><span id="state" class="state"></span> · Stand <span id="time"></span></div>
<h2>Gesamt</h2><div class="big" id="total"></div><div class="track" style="margin:8px 0 4px"><div class="fill" id="totalbar"></div></div><div class="sub" id="totalsub"></div>
<h2>Nach Art</h2><div id="cats"></div>
<h2>Tempo und Netzwerk</h2><div class="kv"><div><b id="rate">–</b><span>Fotos pro Stunde (letzte 30 min)</span></div><div><b id="eta">–</b><span>Restdauer bei diesem Tempo</span></div><div><b id="down">–</b><span>Download, ganzer Mac</span></div><div><b id="up">–</b><span>Upload, ganzer Mac</span></div></div>
<h2>Stapel in Arbeit</h2><div id="batches"></div>
<h2>Protokoll <span id="logname" style="text-transform:none;letter-spacing:0;font-weight:400"></span></h2><pre id="log"></pre>
</main><script>
const NAMES={photo:'Fotos',video:'Videos',favorite:'Favoriten',hidden:'Ausgeblendete'};
const n=x=>x.toLocaleString('de-DE'),gb=b=>(b/1e9).toLocaleString('de-DE',{maximumFractionDigits:1})+' GB';
const mbs=b=>(b/1e6).toLocaleString('de-DE',{maximumFractionDigits:2})+' MB/s';
const row=(label,done,total,extra)=>{const d=document.createElement('div');d.className='row';
  const l=document.createElement('div');l.textContent=label;const t=document.createElement('div');t.className='track';
  const f=document.createElement('div');f.className='fill';f.style.width=(total?done/total*100:0)+'%';t.append(f);
  const r=document.createElement('div');r.className='num';r.textContent=extra;d.append(l,t,r);return d};
async function tick(){let s;try{s=await (await fetch('/status')).json()}catch{document.getElementById('state').textContent='Statusseite nicht erreichbar';return}
  const c=Object.values(s.cats),done=c.reduce((a,x)=>a+x.done,0),total=c.reduce((a,x)=>a+x.total,0),b=c.reduce((a,x)=>a+x.bytes,0),bd=c.reduce((a,x)=>a+x.bytes_done,0);
  const st=document.getElementById('state');st.textContent=s.running?'Lauf aktiv':'Kein Lauf aktiv';st.className='state'+(s.running?'':' stop');
  document.getElementById('time').textContent=s.time;
  document.getElementById('total').textContent=n(done)+' von '+n(total)+' Fotos';
  document.getElementById('totalbar').style.width=(total?done/total*100:0)+'%';
  document.getElementById('totalsub').textContent=(total?(done/total*100).toLocaleString('de-DE',{maximumFractionDigits:1}):0)+' % · '+gb(bd)+' von '+gb(b)+' · '+n(s.files)+' Dateien bei Google (bearbeitete zählen doppelt)';
  const cats=document.getElementById('cats');cats.replaceChildren(...Object.entries(s.cats).map(([k,x])=>row(NAMES[k],x.done,x.total,n(x.done)+' / '+n(x.total)+' · '+gb(x.bytes))));
  document.getElementById('rate').textContent=s.rate_per_hour==null?'–':n(Math.round(s.rate_per_hour));
  const left=total-done;document.getElementById('eta').textContent=s.rate_per_hour>0?(left/s.rate_per_hour>48?n(Math.round(left/s.rate_per_hour/24))+' Tage':n(Math.round(left/s.rate_per_hour))+' Std.'):'–';
  document.getElementById('down').textContent=mbs(s.down);document.getElementById('up').textContent=mbs(s.up);
  const bt=document.getElementById('batches');bt.replaceChildren(...(s.batches.length?s.batches.map(x=>row(x.name,x.done,x.total,x.stage+' · '+n(x.done)+' / '+n(x.total)+' '+(x.unit||'Dateien'))):[Object.assign(document.createElement('div'),{className:'sub',textContent:'Kein Stapel auf der Platte.'})]));
  document.getElementById('logname').textContent=s.log_name?'('+s.log_name+')':'';document.getElementById('log').textContent=s.log.join('\\n')}
tick();setInterval(tick,5000);
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            body, ctype = json.dumps(status()).encode(), "application/json"
        elif self.path == "/":
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    library()
    network()
    print(f"Statusseite: http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
