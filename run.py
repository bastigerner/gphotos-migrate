"""Gesamtlauf iCloud-Fotos -> Google Photos, stapelweise, überlappend und fortsetzbar.

Aufruf: python3 -I -u run.py [--limit N] [--batch 200] [--since 2023] [--hidden] [--refresh] [--dry-run]
  --limit N   höchstens N Fotos in diesem Aufruf (Standard: alle offenen)
  --batch N   Fotos pro Stapel (Standard 200)
  --since D   nur Fotos ab diesem Datum (z. B. 2023 oder 2023-06-01)
  --hidden    NUR die ausgeblendeten Fotos übertragen (Standard: sie werden übersprungen)
  --favorites NUR die Favoriten übertragen (Standard: übersprungen; als letzter Durchgang fahren,
              danach mit album-collect.mjs --favorites in ein Hilfsalbum legen und dort den Stern setzen)
  --variant V nur eine Fassung bearbeiteter Fotos: "original" (die _Original-Kopien) oder "main" (die sichtbare).
              Favoriten: erst --favorites --variant original, dann --favorites --variant main.
  --uuids F   nur die Einträge aus der JSON-Datei F (Liste von Objekten mit "uuid")
  --refresh   Mediathek neu auslesen (dauert Minuten; sonst gilt state/library.json)
  --dry-run   nur zählen, nichts laden oder hochladen

Reihenfolge: erst alle Fotos (klein, schnell), dann die Videos (groß), jeweils nach Datum.
Ein Hintergrund-Faden lädt die nächsten Stapel schon aus iCloud und bereitet sie auf (PhotoFetch.app,
prepare.py), während der Hauptfaden den aktuellen Stapel hochlädt (api-upload.mjs). Höchstens AHEAD fertige Stapel warten auf der Platte.
Der Stand liegt in state/done.jsonl; ein erneuter Aufruf macht dort weiter.
Hält an bei Tageskontingent (Exit 3 von api-upload.mjs) oder beim ersten unerwarteten Fehler.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
WORK = Path.home() / "Pictures" / "gphotos-work"
DONE = STATE / "done.jsonl"
LIBRARY = STATE / "library.json"
MAX_BATCH_BYTES = 4 * 1024**3  # Obergrenze je Stapel, begrenzt den Plattenbedarf
FETCHERS = 4  # gleichzeitige Ladeprozesse je Stapel
AHEAD = 2  # so viele fertig vorbereitete Stapel dürfen auf den Upload warten


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


limit = int(arg("--limit", 10**9))
batch_size = int(arg("--batch", 200))
since = arg("--since", "")
only_hidden = "--hidden" in sys.argv
only_favorites = "--favorites" in sys.argv
variant = arg("--variant", "")  # "original" oder "main": nur diese Fassung bearbeiteter Fotos (siehe prepare.py)
assert variant in ("", "original", "main"), "--variant muss original oder main sein"
only_uuids = {r["uuid"].removesuffix("-original") for r in json.loads(Path(arg("--uuids")).read_text())} if arg("--uuids") else None
dry = "--dry-run" in sys.argv

STATE.mkdir(exist_ok=True)
# Ein Foto gilt als erledigt, wenn ALLE seine Fassungen (bearbeitet und ggf. "-original") eine Google-ID haben.
latest = {}
if DONE.exists():
    for line in DONE.read_text().splitlines():
        r = json.loads(line)
        latest[r["uuid"]] = bool(r.get("id"))  # letzte Zeile gilt: so lässt sich ein Eintrag gezielt wieder öffnen
# Erledigt = die sichtbare Fassung hat eine Google-ID UND eine protokollierte "-original"-Fassung ebenfalls.
done = {u for u, ok in latest.items() if ok and not u.endswith("-original") and latest.get(u + "-original", True)}

# Das Auslesen der Mediathek dauert Minuten – deshalb einmal lesen und in state/library.json halten.
if "--refresh" in sys.argv or not LIBRARY.exists():
    LIBRARY.write_text(subprocess.run(["uvx", "osxphotos", "query", "--json"], capture_output=True, text=True, check=True).stdout)
# Nicht zur eigenen Mediathek gehören: geteilte Alben, Papierkorb und "Mit dir geteilt"-Inhalte aus Nachrichten,
# die nie in die Mediathek gesichert wurden (syndicated ohne saved_to_library) – die gibt Apple auch nicht heraus.
photos = [p for p in json.loads(LIBRARY.read_text())
          if not p.get("shared") and not p.get("intrash") and not (p.get("syndicated") and not p.get("saved_to_library"))]
# Favoriten laufen als eigener, letzter Durchgang (--favorites): so liegen sie bei Google unter "Kürzlich hinzugefügt"
# beieinander und bekommen den Stern dort mit EINER Mehrfachauswahl – statt jedes Foto einzeln im Browser zu öffnen.
todo = [p for p in photos if p["uuid"] not in done and p["date"] >= since
        and (variant != "original" or p.get("hasadjustments"))
        and (p["uuid"] in only_uuids if only_uuids is not None
             else bool(p.get("hidden")) == only_hidden and bool(p.get("favorite")) == only_favorites)]
todo.sort(key=lambda p: (bool(p.get("ismovie")), p["date"]))  # Fotos zuerst, dann Videos
print(f"Mediathek: {len(photos)} eigene Einträge, erledigt: {len(done)}, offen in diesem Modus: {len(todo)} "
      f"(davon Videos: {sum(1 for p in todo if p.get('ismovie'))})")
if dry:
    sys.exit(0)

todo = todo[:limit]
batches = []
while todo:
    batch, size = [], 0
    while todo and len(batch) < batch_size and (not batch or size + (todo[0].get("original_filesize") or 0) <= MAX_BATCH_BYTES):
        size += todo[0].get("original_filesize") or 0
        batch.append(todo.pop(0))
    batches.append((batch, size))

shutil.rmtree(WORK, ignore_errors=True)
ready = queue.Queue(maxsize=AHEAD)
stop = threading.Event()


def produce():
    """Stapel aus iCloud laden und aufbereiten; legt (verzeichnis, text) oder eine Exception in die Warteschlange."""
    try:
        for n, (batch, size) in enumerate(batches, 1):
            if stop.is_set():
                return
            d = WORK / f"b{n:04d}"
            (d / "export").mkdir(parents=True)
            (d / "query.json").write_text(json.dumps(batch))
            uuids = d / "uuids.txt"
            uuids.write_text("\n".join(p["uuid"] for p in batch) + "\n")
            assert uuids.stat().st_size > 36, "leere UUID-Liste"
            # PhotoFetch lädt die Originale direkt über PhotoKit (als App gestartet, damit die Foto-Berechtigung ihr gehört).
            # FETCHERS Ladeprozesse gleichzeitig: gemessen schneller als einer allein (iCloud liefert je Prozess gedrosselt).
            t_fetch = time.time()
            ids = [p["uuid"] for p in batch]
            procs = []
            for i in range(FETCHERS):
                part = ids[i::FETCHERS]
                if part:
                    lst = d / f"uuids{i}.txt"
                    lst.write_text("\n".join(part) + "\n")
                    procs.append((i, subprocess.Popen(["open", "-W", "-n", str(HERE / "PhotoFetch.app"), "--args", str(lst), str(d / f"part{i}"), "6"])))
            fetch = {"errors": []}
            for i, pr in procs:
                pr.wait()
                marker = d / f"part{i}" / "fetch.done"
                # Fehlt die Abschlussdatei (Prozess beendet/abgestürzt), gilt der Teil als unvollständig: was da ist, wird
                # verarbeitet, der Rest bleibt offen und kommt in einem späteren Durchgang wieder.
                part_done = json.loads(marker.read_text()) if marker.exists() else {"errors": [f"Ladeprozess {i} ohne Abschluss beendet"]}
                if part_done.get("error"):
                    raise RuntimeError("PhotoFetch: " + part_done["error"])
                fetch["errors"] += part_done.get("errors", [])
                for f in (d / f"part{i}").iterdir():
                    if not f.name.startswith("fetch."):
                        f.rename(d / "export" / f.name)
                shutil.rmtree(d / f"part{i}")
            t_fetch = time.time() - t_fetch
            prep = subprocess.run([sys.executable, "-I", str(HERE / "prepare.py"), str(d / "query.json"), str(d / "export"), str(d / "out")],
                                  capture_output=True, text=True, check=True, env={**os.environ, "GPHOTOS_VARIANT": variant}).stdout.strip()
            shutil.rmtree(d / "export")  # nur die aufbereiteten Dateien müssen auf den Upload warten
            # Alles, was beim Laden oder Aufbereiten schiefging, dauerhaft festhalten (state/errors.jsonl).
            issues = json.loads((d / "out" / "problems.json").read_text())
            with (STATE / "errors.jsonl").open("a") as ef:
                for stage, lines in (("laden", fetch.get("errors", [])), ("aufbereiten", issues["problems"]), ("warnung", issues["warnings"])):
                    for line in lines:
                        ef.write(json.dumps({"zeit": time.strftime("%Y-%m-%d %H:%M:%S"), "schritt": stage, "meldung": line}, ensure_ascii=False) + "\n")
            info = f"Stapel {n}/{len(batches)}: {len(batch)} Einträge, {size / 1e9:.1f} GB, ab {batch[0]['date'][:10]} | Laden aus iCloud: {t_fetch:.0f} s"
            for e in fetch.get("errors", []):
                info += "\n  Ladefehler: " + e
            ready.put((d, info + "\n  " + prep))
        ready.put(None)
    except Exception as e:  # an den Hauptfaden melden statt still zu sterben
        ready.put(e)


threading.Thread(target=produce, daemon=True).start()
while True:
    item = ready.get()
    if item is None:
        break
    if isinstance(item, Exception):
        sys.exit(f"Angehalten beim Laden: {item}")
    d, info = item
    print(info, flush=True)
    t_up = time.time()
    up = subprocess.run(["node", str(HERE / "api-upload.mjs"), str(d / "out" / "manifest.json")])
    # Google bricht das Anlegen gelegentlich mit 409 ab, legt die Dateien aber trotzdem an: nachtragen, dann den Rest
    # des Stapels hochladen. Höchstens zwei Wiederholungen je Stapel, sonst hält der Lauf wie bisher an.
    for _ in range(2):
        if up.returncode != 1:
            break
        if subprocess.run(["node", str(HERE / "reconcile.mjs"), str(d / "out" / "manifest.json")]).returncode != 0:
            break
        up = subprocess.run(["node", str(HERE / "api-upload.mjs"), str(d / "out" / "manifest.json")])
    print(f"  Upload: {time.time() - t_up:.0f} s", flush=True)
    results = d / "out" / "results.json"
    if results.exists():
        rows = json.loads(results.read_text())
        with DONE.open("a") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with (STATE / "errors.jsonl").open("a") as ef:
            for r in rows:
                if not r.get("id"):
                    ef.write(json.dumps({"zeit": time.strftime("%Y-%m-%d %H:%M:%S"), "schritt": "hochladen",
                                         "meldung": f"{r['uuid']} {r.get('fileName')}: {r.get('error') or r.get('status')}"}, ensure_ascii=False) + "\n")
    if up.returncode != 0:
        stop.set()
        print("Angehalten:", "Tageskontingent erreicht" if up.returncode == 3 else f"Upload-Fehler (Exit {up.returncode})")
        sys.exit(up.returncode)
    shutil.rmtree(d, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)
print("Durchlauf beendet.")
