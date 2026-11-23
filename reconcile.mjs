// Nachtragen nach einem abgebrochenen Upload: Google beantwortet mediaItems:batchCreate gelegentlich mit
// "409 ABORTED", legt die Dateien aber trotzdem an. Ohne Nachtrag würden sie beim nächsten Versuch doppelt hochgeladen.
// Aufruf: node reconcile.mjs <manifest.json>
// Ablauf: audit.mjs listet, was bei Google liegt, aber nicht im Protokoll steht; diese Einträge werden den noch
// offenen Dateien des Stapels über den Dateinamen zugeordnet (nur eindeutige Treffer), in ihre Alben gelegt und in
// results.json des Stapels eingetragen. run.py übernimmt results.json danach wie üblich ins Protokoll.
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { albumBook, knownAlbums, rememberAlbum } from './albums.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const manifestPath = process.argv[2];
const resultsPath = join(dirname(manifestPath), 'results.json');
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const results = existsSync(resultsPath) ? JSON.parse(readFileSync(resultsPath, 'utf8')) : [];

// Erst sichern, was der abgebrochene Upload schon gemeldet hat, damit der Abgleich es als protokolliert sieht.
const logged = new Set(results.filter(r => r.id).map(r => r.uuid));
const doneFile = join(here, 'state', 'done.jsonl');
const doneIds = new Set((existsSync(doneFile) ? readFileSync(doneFile, 'utf8') : '').split('\n').filter(Boolean).map(l => JSON.parse(l).id).filter(Boolean));
const unsaved = results.filter(r => r.id && !doneIds.has(r.id));
if (unsaved.length) writeFileSync(doneFile, unsaved.map(r => JSON.stringify(r)).join('\n') + '\n', { flag: 'a' });

execFileSync('node', [join(here, 'audit.mjs')], { stdio: 'ignore' });
const extra = JSON.parse(readFileSync(join(here, 'state', 'audit.json'), 'utf8')).extra;
const pending = manifest.filter(i => !logged.has(i.uuid));
const count = (list, key) => list.reduce((m, x) => m.set(key(x), (m.get(key(x)) ?? 0) + 1), new Map());
const pendingNames = count(pending, i => i.fileName), extraNames = count(extra, e => e.filename);
const matched = [];
for (const e of extra) {
  if (pendingNames.get(e.filename) !== 1 || extraNames.get(e.filename) !== 1) continue;
  matched.push({ item: pending.find(i => i.fileName === e.filename), id: e.id });
}
console.log(`  Nachtrag: bei Google ohne Protokoll ${extra.length}, offen im Stapel ${pending.length}, eindeutig zugeordnet ${matched.length}`);
if (!matched.length) process.exit(0);

const secret = JSON.parse(readFileSync(join(here, 'client_secret.json'), 'utf8'));
const { client_id, client_secret } = secret.installed ?? secret.web;
const { refresh_token } = JSON.parse(readFileSync(join(here, 'token.json'), 'utf8'));
const tok = await (await fetch('https://oauth2.googleapis.com/token', { method: 'POST', body: new URLSearchParams({ client_id, client_secret, grant_type: 'refresh_token', refresh_token }) })).json();
const post = async (path, body) => {
  const res = await fetch('https://photoslibrary.googleapis.com/v1' + path, { method: 'POST', headers: { Authorization: `Bearer ${tok.access_token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`${path}: ${res.status} ${JSON.stringify(json)}`);
  return json;
};
const byAlbum = new Map();
for (const { item, id } of matched) for (const title of item.albums) byAlbum.set(title, [...(byAlbum.get(title) ?? []), id]);
const albumName = albumBook(here);
for (const [apple, ids] of byAlbum) {
  const title = albumName(apple, ids.length);
  let albumId = knownAlbums(here)[title];
  if (!albumId) { albumId = (await post('/albums', { album: { title } })).id; rememberAlbum(here, title, albumId); }
  for (let i = 0; i < ids.length; i += 50) await post(`/albums/${albumId}:batchAddMediaItems`, { mediaItemIds: ids.slice(i, i + 50) });
}
for (const { item, id } of matched) {
  results.push({ uuid: item.uuid, fileName: item.fileName, kind: item.kind, favorite: item.favorite, hidden: !!item.hidden, albums: item.albums, id, nachgetragen: 'batchCreate 409, per Abgleich zugeordnet' });
}
writeFileSync(resultsPath, JSON.stringify(results, null, 1));
