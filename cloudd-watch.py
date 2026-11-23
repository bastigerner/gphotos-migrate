"""Wächter gegen hängende iCloud-Downloads. Start: python3 -I -u cloudd-watch.py

Der iCloud-Dienst cloudd bleibt zeitweise auf einer Verbindung hängen, die nur noch rund 1 Mbit/s liefert; ein
Neustart des Dienstes (macOS startet ihn sofort selbst wieder) baut die Verbindung neu auf und löst die Bremse.
Der Wächter misst jede Minute, wie viele Bytes cloudd empfangen hat. Lädt PhotoFetch gerade und liegt die Rate
LIMIT_MINUTES Minuten in Folge unter LIMIT_MBIT, beendet er cloudd und prüft drei Minuten lang, ob es geholfen hat.
Geholfen: COOLDOWN Minuten Ruhe. Nicht geholfen: Pause bis zum nächsten Versuch verdoppeln, höchstens BACKOFF_MAX.
"""
import subprocess
import time
from datetime import datetime
from pathlib import Path

LIMIT_MBIT = 2.0     # darunter gilt der Download als gebremst (gemessen: gebremst ~1, normal 10 und mehr)
LIMIT_MINUTES = 5    # so lange muss die Rate am Stück darunter liegen
COOLDOWN = 10        # Minuten Pause nach einem Neustart, der geholfen hat
BACKOFF_MAX = 120    # Minuten: längste Pause, wenn Neustarts nichts bringen
LOG = Path(__file__).parent / "state" / "cloudd-watch.log"


def log(text):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def received():
    """Summe der von allen cloudd-Prozessen empfangenen Bytes (Zähler seit Prozessstart)."""
    out = subprocess.run(["nettop", "-P", "-x", "-l", "1", "-J", "bytes_in", "-p", "cloudd"],
                         capture_output=True, text=True).stdout
    return sum(int(line.split()[-1]) for line in out.splitlines() if line.startswith("cloudd."))


def loading():
    return subprocess.run(["pgrep", "-f", "PhotoFetch.app/Contents"], capture_output=True).returncode == 0


def rate(minutes=1):
    """Mittlere Empfangsrate in Mbit/s; None, wenn cloudd währenddessen neu startete (Zähler begann von vorn)."""
    # Der Zähler ist die Summe über cloudds offene Verbindungen; schließt eine, sinkt er. Dann noch einmal messen.
    for _ in range(3):
        before = received()
        time.sleep(minutes * 60)
        delta = received() - before
        if delta >= 0:
            return delta * 8 / 1e6 / (minutes * 60)
    return None


log(f"Wächter gestartet: Neustart von cloudd bei unter {LIMIT_MBIT} Mbit/s über {LIMIT_MINUTES} min")
slow, pause = 0, COOLDOWN
while True:
    mbit = rate()
    slow = slow + 1 if mbit is not None and mbit < LIMIT_MBIT and loading() else 0
    if slow < LIMIT_MINUTES:
        continue
    log(f"{slow} min unter {LIMIT_MBIT} Mbit/s (zuletzt {mbit:.1f}) – starte cloudd neu")
    subprocess.run(["killall", "cloudd"])
    time.sleep(30)  # cloudd neu starten und Verbindungen aufbauen lassen
    after = rate(3)
    helped = after is not None and after >= LIMIT_MBIT
    # Nicht dauerhaft neu starten: Hilft ein Neustart nicht, liegt es nicht an der hängenden Verbindung. Dann die
    # Pause bis zum nächsten Versuch jedes Mal verdoppeln (höchstens BACKOFF_MAX); nach einem Erfolg zurück auf COOLDOWN.
    pause = COOLDOWN if helped else min(max(pause, 15) * 2, BACKOFF_MAX)
    shown = "nicht messbar" if after is None else f"{after:.1f} Mbit/s"
    log(f"danach {shown} – {'geholfen' if helped else 'nicht geholfen'}, nächster Neustart frühestens in {pause} min")
    time.sleep(pause * 60)
    slow = 0
