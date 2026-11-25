# gphotos-migrate

Zieht eine komplette Apple-Fotos-Mediathek (iCloud) nach Google Photos um – auf dem Mac, über die offizielle
Google Photos Library API, ohne Umweg über Takeout oder einen Handy-Export.

**Stand:** an genau einer Mediathek erprobt. Nutzung auf eigene Gefahr; erst mit `--limit` im Kleinen testen.
Das Projekt löscht nichts – weder in Apple Fotos noch bei Google.

## Was übertragen wird

- Fotos und Videos in Originalqualität, geladen direkt aus iCloud (PhotoKit), stapelweise und fortsetzbar
- Aufnahmedatum und Ort – wo sie nur in Apples Datenbank stehen, werden sie in die Datei geschrieben
- Beschreibungen
- Live Photos als Google-„Motion Photo" (Bild und Video in einer Datei)
- Alben mit gleichem Namen; Alben über Googles Grenze von 20.000 Einträgen werden auf Folgealben verteilt
  oder – wenn in `split-albums.json` genannt – nach Aufnahmejahr geteilt („Name 2021", „Name 2022" …)
- bearbeitete Fotos in beiden Fassungen: die bearbeitete wie in Apple Fotos, das Original zusätzlich als
  `…_Original` nur in der Mediathek

## Was die Google-API nicht kann (und wie es trotzdem geht)

| Grenze der API | Lösung hier |
|---|---|
| keine Favoriten-Sterne setzen | Favoriten laufen als eigener Durchgang; `album-collect.mjs --favorites` legt sie in ein Hilfsalbum, dort alle markieren und Stern setzen |
| nicht in den gesperrten Ordner hochladen | `album-collect.mjs --hidden` legt die ausgeblendeten Fotos in ein Hilfsalbum, von dort von Hand verschieben |
| nichts löschen, keine Alben löschen | Protokoll und Abgleich verhindern Dubletten nach Abbrüchen; leere Hilfsalben löscht man von Hand |
| nur selbst hochgeladene Dateien sichtbar | der Abgleich (`audit.mjs`) vergleicht genau diese mit dem Protokoll |

## Voraussetzungen

- macOS mit Apple Fotos und eingeschalteter iCloud-Fotomediathek
- Xcode Command Line Tools (`xcode-select --install`) für `swiftc`
- [Node.js](https://nodejs.org) ab Version 20, Python 3
- `ffmpeg`, `exiftool` und `uv` (für `uvx osxphotos`), z. B. `brew install ffmpeg exiftool uv`
- ein Google-Konto mit genug Speicher und ein eigenes Google-Cloud-Projekt (kostenlos, siehe unten)
- freier Platz auf dem Mac: Fotos behält die aus iCloud geladenen Originale in der Mediathek, auch bei
  „Mac-Speicher optimieren“ (hier belegte sie danach 173 GB). Nach dem Umzug iCloud-Fotos in Fotos ausschalten und
  die Mediathek löschen – in iCloud und bei Google bleibt alles erhalten.

Es gibt keine npm-Abhängigkeiten.

## Einrichtung

1. **Lade-App bauen:** `./build.sh` – erzeugt `PhotoFetch.app`. Beim ersten Lauf fragt macOS nach dem Zugriff
   auf Fotos.
2. **Google-Zugang anlegen** (jeder braucht einen eigenen):
   - in der [Google Cloud Console](https://console.cloud.google.com) ein Projekt anlegen
   - die „Photos Library API" aktivieren
   - OAuth-Zustimmungsbildschirm einrichten (Typ „Extern", Modus „Test" genügt) und das eigene Google-Konto
     als Testnutzer eintragen
   - Anmeldedaten → OAuth-Client-ID vom Typ „Desktop-App" erstellen, die JSON-Datei herunterladen und als
     `client_secret.json` in diesen Ordner legen
3. **Optional:** `split-albums.example.json` nach `split-albums.json` kopieren und die Namen der Alben eintragen,
   die nach Jahren geteilt werden sollen.

Die Anmeldung bei Google passiert beim ersten Upload und beim ersten Abgleich von selbst: Der Browser öffnet
sich, nach der Freigabe liegen `token.json` (Hochladen) und `token-audit.json` (nur Lesen) im Ordner.
Diese Dateien, `client_secret.json`, `albums.json` und der Ordner `state/` sind persönlich und stehen in
`.gitignore`.

## Ablauf

```sh
# 1. Probelauf mit wenigen Fotos, dann bei Google ansehen
caffeinate -i python3 -I -u run.py --limit 20

# 2. Hauptlauf (Fotos zuerst, dann Videos). Beliebig oft neu starten: Erledigtes wird übersprungen.
caffeinate -i python3 -I -u run.py

# 3. Abgleich: liegt bei Google, was im Protokoll steht?
node audit.mjs

# 4. Favoriten zuletzt – erst die Originale bearbeiteter Favoriten, dann die sichtbaren Fassungen
caffeinate -i python3 -I -u run.py --favorites --variant original
caffeinate -i python3 -I -u run.py --favorites --variant main
node album-collect.mjs --favorites

# 5. Ausgeblendete Fotos (siehe „Bekannte Grenzen")
caffeinate -i python3 -I -u run.py --hidden
node album-collect.mjs --hidden
```

Jeden Durchgang genau so oft wiederholen, bis `--dry-run` „offen in diesem Modus: 0" meldet. Ausnahme:
`--favorites --variant original` nur **einmal** fahren – diese Fassungen gelten erst als erledigt, wenn auch die
sichtbare Fassung oben ist, ein zweiter Lauf lüde sie doppelt hoch.

Nebenher:

- `python3 -I -u status.py` – Statusseite auf http://127.0.0.1:8899 (liest nur lokale Dateien)
- `python3 -I -u cloudd-watch.py` – **optional und bewusst einzuschalten:** startet den iCloud-Dienst `cloudd`
  neu, wenn der Download minutenlang fast steht. Das beendet einen Systemdienst (macOS startet ihn sofort
  wieder) und kann einzelne laufende Downloads abbrechen; sie werden beim nächsten Lauf nachgeholt.

## Die Dateien

| Datei | Aufgabe |
|---|---|
| `run.py` | Gesamtlauf: Stapel bilden, laden, aufbereiten, hochladen, protokollieren |
| `fetch-src/main.swift` | PhotoFetch: lädt Originale über PhotoKit aus iCloud |
| `prepare.py` | Datum/Ort ergänzen, Live Photos zusammenbauen, Upload-Liste schreiben |
| `api-upload.mjs` | Hochladen, Alben anlegen, Sonderfälle (Dubletten, abgelehnte Videos) |
| `reconcile.mjs` | nach einem Abbruch von Google nachtragen, was trotzdem angelegt wurde |
| `audit.mjs` | Abgleich Google ↔ Protokoll ↔ Apple-Mediathek |
| `album-collect.mjs`, `album-merge.mjs`, `split-album.mjs`, `api-album-add.mjs` | Album-Werkzeuge |
| `status.py`, `cloudd-watch.py` | Statusseite, Wächter für hängende Downloads |

Zustand liegt in `state/`: `library.json` (Abbild der Mediathek), `done.jsonl` (Protokoll: was hat welche
Google-Kennung), `errors.jsonl`, `audit.json`.

## Was unterwegs gelernt wurde

- Google beantwortet das Anlegen manchmal mit einem Fehler und legt die Dateien trotzdem an. Deshalb wird
  nie blind wiederholt, sondern erst abgeglichen (`reconcile.mjs`).
- Bytegleiche Dateien im selben Aufruf lehnt Google ab; im zweiten Aufruf liefert es die Kennung des Zwillings.
  Google führt Dubletten zusammen – die Zahl der Dateien bei Google ist deshalb kleiner als die in Apple Fotos.
- MP4 ohne Tonspur mit der Kennung `mp4v` werden abgelehnt und nach verlustfreiem Neuverpacken angenommen.
- MP4 ohne Datum im Dateikopf bekämen bei Google den Tag des Hochladens; `prepare.py` setzt das Datum dort.
- Der iCloud-Download bleibt zeitweise auf einer sehr langsamen Verbindung hängen; ein Neustart von `cloudd`
  löst das (dafür der Wächter).

## Bekannte Grenzen

- **Ausgeblendete Fotos** gibt macOS nur heraus, wenn in Fotos → Einstellungen der Schutz des Albums
  „Ausgeblendet" (Touch ID/Passwort) ausgeschaltet ist. Bis zum Verschieben in den gesperrten Ordner sind sie
  bei Google normal sichtbar.
- **Sehr kurze Videos** (Bruchteile einer Sekunde) und **beschädigte Videos** nimmt Google an und verwirft sie
  dann wieder. `audit.mjs` meldet sie als „fehlt bei Google"; sie müssen von Hand neu kodiert werden.
- **Abweichendes Datum:** Hat ein Foto in Apple Fotos ein anderes Datum als das Aufnahmedatum in der Datei,
  zeigt Google das aus der Datei. `audit.mjs` listet die Abweichungen; die meisten sind reine
  Zeitzonen-Darstellung.
- Im Modus „Test" des OAuth-Zustimmungsbildschirms kann Googles Anmeldung nach einiger Zeit ablaufen; dann
  `token.json` löschen und neu anmelden.
- Die Datumskorrektur für MP4 ohne Datum im Dateikopf ist als Einzelfall gegen Google geprüft; der im Skript
  eingebaute Weg wurde danach nur lokal an den Dateifeldern geprüft, nicht mit einem weiteren Upload.
- Ausgaben und Kommentare sind deutsch.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
