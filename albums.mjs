// Google erlaubt höchstens 20.000 Einträge je Album. Größere Apple-Alben werden deshalb auf Folgealben verteilt:
// "WhatsApp", "WhatsApp (2)", "WhatsApp (3)" … Gezählt wird, was laut state/done.jsonl schon in einem Album liegt.
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const LIMIT = 20000;
export function albumBook(here) {
  const latest = new Map();
  const doneFile = join(here, 'state', 'done.jsonl');
  for (const line of (existsSync(doneFile) ? readFileSync(doneFile, 'utf8') : '').split('\n').filter(Boolean)) { const r = JSON.parse(line); latest.set(r.uuid, r); }
  const counts = new Map();
  for (const r of latest.values()) if (r.id) for (const a of r.albums ?? []) counts.set(a, (counts.get(a) ?? 0) + 1);
  // Name des Google-Albums, in das die nächsten `adding` Einträge des Apple-Albums `title` gehören.
  return function name(title, adding) {
    let c = counts.get(title) ?? 0, idx = Math.floor(c / LIMIT);
    if ((c % LIMIT) + adding > LIMIT) { idx++; c = idx * LIMIT; }  // passt nicht mehr ganz hinein: nächstes Album beginnen
    counts.set(title, c + adding);
    return idx ? `${title} (${idx + 1})` : title;
  };
}

// albums.json (Albumtitel -> Google-Album-ID) verhindert, dass ein Album zweimal angelegt wird. Mehrere Skripte
// schreiben hinein, teils gleichzeitig (Upload, Nachtrag, Album-Aufteilung). Deshalb nie den eigenen, beim Start
// gelesenen Stand zurückschreiben, sondern vor jedem Nachschlagen und Schreiben die Datei frisch lesen – sonst
// überschreibt ein Skript die Einträge des anderen, und das nächste legt dasselbe Album noch einmal an.
const albumsFile = here => join(here, 'albums.json');
export const knownAlbums = here => existsSync(albumsFile(here)) ? JSON.parse(readFileSync(albumsFile(here), 'utf8')) : {};
export function rememberAlbum(here, title, id) {
  writeFileSync(albumsFile(here), JSON.stringify({ ...knownAlbums(here), [title]: id }, null, 1));
}
