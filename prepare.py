"""Bereitet die von PhotoFetch geladenen Dateien für den API-Upload vor und schreibt manifest.json.

Aufruf: prepare.py <query.json> <exportverzeichnis> <ausgabeverzeichnis>
Erwartet: {uuid}.<ext> (Original), {uuid}.mov (Live-Video), {uuid}_edited.<ext> (bearbeitet).

- Datum/Ort aus der Apple-Mediathek werden nur dort in die Datei geschrieben, wo sie fehlen.
- Live Photos werden zu einer Datei mit eingebettetem MP4 (Google Motion Photo) zusammengebaut.
- Bearbeitete Fotos gehen in beiden Fassungen ins Manifest.

Tempo: exiftool wird je Stapel nur wenige Male gestartet (ein Lesedurchgang, ein Schreibdurchgang per
Argumentdatei) statt mehrfach pro Datei; ffmpeg läuft parallel; Dateien werden verschoben statt kopiert.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

query, src, dst = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
dst.mkdir(parents=True, exist_ok=True)
IMG = {".heic", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".dng", ".webp", ".avif"}
VID = {".mov", ".mp4", ".m4v"}
MOTION_OK = {".heic", ".jpg", ".jpeg"}
VARIANT = os.environ.get("GPHOTOS_VARIANT", "")
# Apple-Alben mit mehr Einträgen, als Google je Album zulässt (20.000), werden nach Aufnahmejahr geteilt:
# "WhatsApp" -> "WhatsApp 2021". Die Liste dieser Alben steht in split-albums.json neben diesem Skript
# (Vorlage: split-albums.example.json).
_split = Path(__file__).parent / "split-albums.json"
SPLIT = set(json.loads(_split.read_text())) if _split.exists() else set()
t0 = time.time()
timing = {}


def lap(name):
    global t0
    timing[name] = round(time.time() - t0, 1)
    t0 = time.time()


def exiftool_read(files, *tags):
    """Ein exiftool-Aufruf für viele Dateien; liefert {pfad: {tag: wert}}."""
    if not files:
        return {}
    argfile = dst / ".read.args"
    argfile.write_text("\n".join(str(f) for f in files) + "\n")
    out = subprocess.run(["exiftool", "-json", "-n", "-m", "-q", "-q", *tags, "-@", str(argfile)], capture_output=True, text=True).stdout
    argfile.unlink()
    return {r["SourceFile"]: r for r in json.loads(out or "[]")}


def exiftool_write(commands):
    """commands: Liste von (pfad, [tag-zuweisungen]); EIN exiftool-Aufruf für alle."""
    commands = [c for c in commands if c[1]]
    if not commands:
        return
    argfile = dst / ".write.args"
    lines = []
    for path, tags in commands:
        lines += [*tags, str(path), "-execute"]
    argfile.write_text("\n".join(lines) + "\n")
    # -m: defekte Kamera-Metadaten alter Dateien sind kein Abbruchgrund
    subprocess.run(["exiftool", "-@", str(argfile), "-common_args", "-overwrite_original", "-q", "-q", "-m", "-api", "QuickTimeUTC=1"])
    argfile.unlink()


def same_instant(exif_date, apple_iso):
    """True, wenn ein exiftool-Datum mit Zeitzone ("2023:08:19 14:44:53+02:00" oder "…Z") denselben Moment meint wie Apple."""
    if not isinstance(exif_date, str) or len(exif_date) < 19:
        return False
    tz = exif_date[19:].split(".")[-1] if "." in exif_date[19:] and exif_date[19] == "." else exif_date[19:]
    tz = tz[tz.find("+") if "+" in tz else tz.find("-") if "-" in tz else tz.find("Z"):] if any(c in tz for c in "+-Z") else ""
    if not tz:
        return False
    try:
        d = datetime.fromisoformat(exif_date[:10].replace(":", "-") + "T" + exif_date[11:19] + ("+00:00" if tz == "Z" else tz))
        return abs((d - datetime.fromisoformat(apple_iso)).total_seconds()) <= 2
    except ValueError:
        return False


# ---- 1. Planen: welche Dateien gehören zu welcher Fassung -------------------------------------------
by_uuid = {}
for f in sorted(src.iterdir()):
    # Apple liefert bearbeitete Fassungen gelegentlich als JPEG mit der Endung ".heic". exiftool verweigert dann das
    # Schreiben (kein Datum bei Google) und der Live-Photo-Bau nähme das falsche Format: Endung dem Inhalt anpassen.
    if f.suffix.lower() in IMG - {".jpg", ".jpeg"} and f.open("rb").read(3) == b"\xff\xd8\xff":
        f = f.rename(f.with_suffix(".jpg"))
    if f.suffix.lower() in IMG | VID:
        by_uuid.setdefault(f.name[:36], []).append(f)

plans, problems, apple = [], [], {}
for p in json.load(open(query)):
    uuid = p["uuid"]
    files = by_uuid.get(uuid, [])
    for f in files:
        apple[str(f)] = p
    # Ein geändertes Titelbild legt Apple als Standbild (fullSizePhoto) ab; das ist keine bearbeitete Fassung des Videos.
    if p.get("ismovie"):
        files = [f for f in files if f.suffix.lower() in VID]
    if not files:
        problems.append(f"{uuid} {p['original_filename']}: keine geladene Datei")
        continue

    def pick(exts, want_edited):
        match = sorted(f for f in files if f.suffix.lower() in exts)
        edited = [f for f in match if "_edited" in f.stem]
        return (edited or match) if want_edited else [f for f in match if f not in edited]

    stem = Path(p["original_filename"]).stem
    has_edit = any("_edited" in f.stem for f in files)
    # Bearbeitete Fotos gehen in beiden Fassungen zu Google: die bearbeitete wie in Apple Fotos
    # (mit Alben und Favorit), das Original zusätzlich nur in die Mediathek.
    for original_copy in ([False, True] if has_edit else [False]):
        # GPHOTOS_VARIANT=original|main beschränkt den Lauf auf eine Fassung (für den Favoriten-Durchgang:
        # erst die "_Original"-Kopien, dann die sichtbaren Fassungen als zusammenhängender Block).
        if VARIANT and (VARIANT == "original") != original_copy:
            continue
        images, videos = pick(IMG, not original_copy), pick(VID, not original_copy)
        key = uuid + ("-original" if original_copy else "")
        if not images and not videos:
            problems.append(f"{key} {p['original_filename']}: keine geladene Datei für diese Fassung")
            continue
        live = bool(images and videos and p.get("live_photo") and images[0].suffix.lower() in MOTION_OK)
        main = images[0] if images else videos[0]
        plans.append({
            "key": key, "main": main, "video": videos[0] if live else None, "live": live,
            "shared": has_edit,  # Datei kann von beiden Fassungen gebraucht werden -> kopieren statt verschieben
            "entry": {
                "uuid": key,
                "albums": [] if original_copy else [f"{a} {p['date'][:4]}" if a in SPLIT else a for a in (p.get("albums") or [])],
                "favorite": bool(p.get("favorite")) and not original_copy,
                "description": p.get("description") or "",
                "hidden": bool(p.get("hidden")),
                # Path(name).stem lässt einen Namen ohne Endung unverändert; die Endung kommt immer von der geladenen Datei.
                "fileName": stem + ("_Original" if original_copy else "") + main.suffix,
                "kind": "live" if live else "live-ohne-video" if p.get("live_photo") and images else "normal",
            },
        })
lap("planen")

# ---- 2. Fehlendes Datum / fehlenden Ort ergänzen (ein Lese-, ein Schreibdurchgang) -----------------
# Apple führt beides für manche Dateien nur in der Datenbank; Google liest nur die Datei.
used = sorted({pl["main"] for pl in plans}, key=str)
have = exiftool_read(used, "-DateTimeOriginal", "-GPSLatitude", "-Keys:CreationDate", "-QuickTime:CreateDate")
writes = []
for f in used:
    p, h = apple[str(f)], have.get(str(f), {})
    date = p.get("date") or ""
    if len(date) < 19:
        continue
    local = date[:19].replace("-", ":").replace("T", " ")
    offset = date[-6:] if date[-6] in "+-" else ""
    tags = []
    if f.suffix.lower() in VID:
        # Videos ohne Zeitzonen-Datum (z. B. WhatsApp-MP4) zeigt Google sonst in UTC, also um die Zeitzone versetzt.
        # Das Apple-Datum MIT Offset als Keys:CreationDate eintragen – so wie iPhone-Videos es von Haus aus tragen.
        # Auch überschreiben, wenn die Datei zwar ein solches Datum trägt, es aber nicht denselben Moment meint wie
        # Apple (manche Apps schreiben Ortszeit und kennzeichnen sie als UTC).
        if not same_instant(h.get("CreationDate"), date):
            tags.append(f"-Keys:CreationDate={local}{offset}")
        # Manche MP4 (z. B. aus TikTok gespeicherte) tragen im Dateikopf gar kein Datum ("0000:00:00 …"). Bei denen
        # übergeht Google auch Keys:CreationDate und zeigt den Tag des Hochladens. Dann zusätzlich die Datumsfelder im
        # Dateikopf setzen (exiftool rechnet sie dank QuickTimeUTC in die dort übliche Weltzeit um).
        if str(h.get("CreateDate") or "0000").startswith("0000"):
            tags += [f"-{t}={local}{offset}" for t in ("QuickTime:CreateDate", "QuickTime:ModifyDate", "TrackCreateDate", "MediaCreateDate")]
    else:
        if "DateTimeOriginal" not in h:
            tags += [f"-DateTimeOriginal={local}", f"-CreateDate={local}"] + ([f"-OffsetTimeOriginal={offset}"] if offset else [])
        lat, lon = p.get("latitude"), p.get("longitude")
        if "GPSLatitude" not in h and lat is not None and lon is not None:
            tags += [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                     f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]
    writes.append((f, tags))
exiftool_write(writes)
lap("metadaten")

# ---- 3. Live Photos: Video nach MP4 umpacken (parallel), dann XMP in einem Rutsch --------------------
lives = [pl for pl in plans if pl["live"]]
track = exiftool_read(sorted({pl["video"] for pl in lives}, key=str), "-ee", "-G1", "-StillImageTime", "-TrackDuration")


def keyframe_us(video):
    """Zeitpunkt des Standbilds im Live-Photo-Video; -1 = unbekannt."""
    r = track.get(str(video), {})
    for k, v in r.items():
        if k.endswith(":StillImageTime") and v == -1:
            dur = r.get(k.split(":")[0] + ":TrackDuration")
            if dur is not None:
                return str(round(float(dur) * 1_000_000))
    return "-1"


def remux(pl):
    mp4 = dst / f"{pl['key']}.tmp.mp4"
    if subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(pl["video"]), "-map", "0:v:0", "-map", "0:a:0?",
                    "-c", "copy", "-movflags", "+faststart", str(mp4)]).returncode:
        return None  # z. B. Live-Video ohne Bildspur (defekt in der Mediathek)
    return mp4


with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
    mp4s = list(pool.map(remux, lives))
# Lässt sich das Video nicht umwandeln, geht das Foto als Standbild zu Google (mit Warnung) statt den Stapel anzuhalten.
for pl, mp4 in zip(lives, mp4s):
    if mp4 is None:
        pl["live"] = False
        pl["entry"]["kind"] = "live-ohne-video"
lives, mp4s = [pl for pl in lives if pl["live"]], [m for m in mp4s if m]
lap("ffmpeg")

xmp = []
for pl, mp4 in zip(lives, mp4s):
    out = dst / f"{pl['key']}{pl['main'].suffix}"
    shutil.copyfile(pl["main"], out) if pl["shared"] else os.replace(pl["main"], out)
    heic = out.suffix.lower() == ".heic"
    directory = (
        "[{Item={Mime=%s,Semantic=Primary,Length=0,Padding=%d}},"
        "{Item={Mime=video/mp4,Semantic=MotionPhoto,Length=%d,Padding=0}}]"
        % ("image/heic" if heic else "image/jpeg", 8 if heic else 0, mp4.stat().st_size)
    )
    xmp.append((out, ["-XMP-GCamera:MotionPhoto=1", "-XMP-GCamera:MotionPhotoVersion=1",
                      "-XMP-GCamera:MotionPhotoPresentationTimestampUs=" + keyframe_us(pl["video"]),
                      "-XMP-GContainer:ContainerDirectory=" + directory]))
    pl["out"], pl["mp4"], pl["heic"] = out, mp4, heic
exiftool_write(xmp)
for pl in lives:
    data = pl["mp4"].read_bytes()
    pl["mp4"].unlink()
    with open(pl["out"], "ab") as f:
        if pl["heic"]:  # Googles HEIC-Variante: Video in einer 'mpvd'-Box
            f.write(struct.pack(">I", 8 + len(data)) + b"mpvd")
        f.write(data)
lap("live-bau")

# ---- 4. Alles andere unverändert übernehmen (verschieben, wo die Datei nicht doppelt gebraucht wird) -
for pl in plans:
    if not pl["live"]:
        out = dst / f"{pl['key']}{pl['main'].suffix}"
        shutil.copyfile(pl["main"], out) if pl["shared"] else os.replace(pl["main"], out)
        pl["out"] = out
lap("übernehmen")

manifest = [{**pl["entry"], "file": str(pl["out"])} for pl in plans]
json.dump(manifest, open(dst / "manifest.json", "w"), ensure_ascii=False, indent=1)
# Für das Fehlerprotokoll des Gesamtlaufs: was nicht ins Manifest kam und was nur eingeschränkt ankam.
warnings = [f"{e['uuid']} {e['fileName']}: Live Photo ohne Video, geht als Standbild zu Google" for e in manifest if e["kind"] == "live-ohne-video"]
json.dump({"problems": problems, "warnings": warnings}, open(dst / "problems.json", "w"), ensure_ascii=False, indent=1)
kinds = {}
for e in manifest:
    kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
print("manifest:", len(manifest), kinds, "Probleme:", len(problems), "| Sekunden:", timing)
for line in problems:
    print("  ", line)
