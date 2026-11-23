// Legt per API ein Album an und fügt vorhandene, von diesem Projekt hochgeladene Einträge hinzu.
// Aufruf: node api-album-add.mjs "<albumtitel>" <json-array mit {id, fileName}>
// Zweck: Einträge zum Löschen sammeln – die API selbst kann nicht löschen, aber so reicht bei Google ein "alles auswählen".
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const here = dirname(fileURLToPath(import.meta.url));
const secret = JSON.parse(readFileSync(join(here, 'client_secret.json'), 'utf8'));
const s = secret.installed ?? secret.web;
const { refresh_token } = JSON.parse(readFileSync(join(here, 'token.json'), 'utf8'));
const tok = await (await fetch('https://oauth2.googleapis.com/token', { method: 'POST', body: new URLSearchParams({ client_id: s.client_id, client_secret: s.client_secret, grant_type: 'refresh_token', refresh_token }) })).json();
const call = async (path, body) => {
  const res = await fetch('https://photoslibrary.googleapis.com/v1' + path, { method: 'POST', headers: { Authorization: `Bearer ${tok.access_token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return { ok: res.ok, status: res.status, json: await res.json() };
};
const [title, file] = process.argv.slice(2);
const rows = JSON.parse(readFileSync(file, 'utf8')).filter(r => r.id);
const album = await call('/albums', { album: { title } });
if (!album.ok) throw new Error('Album: ' + JSON.stringify(album.json));
let added = 0;
for (const r of rows) { // einzeln, damit ein schon gelöschter Eintrag die anderen nicht mitreißt
  const a = await call(`/albums/${album.json.id}:batchAddMediaItems`, { mediaItemIds: [r.id] });
  if (a.ok) added++; else console.log('nicht hinzugefügt:', r.fileName, a.status, a.json.error?.message ?? '');
}
console.log(`Album "${title}": ${added} von ${rows.length} hinzugefügt | ${album.json.productUrl}`);
