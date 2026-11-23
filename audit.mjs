// Abgleich: Was liegt laut Google wirklich dort – verglichen mit state/done.jsonl und der Apple-Mediathek.
// Aufruf: node audit.mjs
// Fragt per API alle von diesem Projekt hochgeladenen Einträge ab (nur lesend, eigenes Token in token-audit.json,
// damit der laufende Upload nicht gestört wird) und schreibt state/audit.json.
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const TOKEN = join(here, 'token-audit.json');
const SCOPE = 'https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata';
const secret = JSON.parse(readFileSync(join(here, 'client_secret.json'), 'utf8'));
const { client_id, client_secret } = secret.installed ?? secret.web;

async function tokenRequest(params) {
  const res = await fetchRetry('https://oauth2.googleapis.com/token', { method: 'POST', body: new URLSearchParams({ client_id, client_secret, ...params }) });
  const json = await res.json();
  if (!res.ok) throw new Error('Token-Anfrage fehlgeschlagen: ' + JSON.stringify(json));
  return json;
}
async function accessToken() {
  if (existsSync(TOKEN)) return (await tokenRequest({ grant_type: 'refresh_token', refresh_token: JSON.parse(readFileSync(TOKEN, 'utf8')).refresh_token })).access_token;
  const [code, redirect_uri] = await new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1');
      res.end('Freigabe erhalten, dieses Fenster kann zu.');
      server.close();
      url.searchParams.get('code') ? resolve([url.searchParams.get('code'), server.redirect]) : reject(new Error('Freigabe abgelehnt'));
    }).listen(0, '127.0.0.1', () => {
      server.redirect = `http://127.0.0.1:${server.address().port}`;
      console.log('Freigabe im Browser bestätigen (nur Lesen der eigenen Uploads).');
      execFile('open', ['https://accounts.google.com/o/oauth2/v2/auth?' + new URLSearchParams({ client_id, redirect_uri: server.redirect, response_type: 'code', scope: SCOPE, access_type: 'offline', prompt: 'consent' })]);
    });
  });
  const json = await tokenRequest({ grant_type: 'authorization_code', code, redirect_uri });
  writeFileSync(TOKEN, JSON.stringify({ refresh_token: json.refresh_token }), { mode: 0o600 });
  return json.access_token;
}

// Alle Anfragen hier lesen nur (oder holen ein Token) und sind gefahrlos wiederholbar. Google antwortet zeitweise
// mit 5xx oder 429; ohne Wiederholung bräche der Abgleich ab und mit ihm der Nachtrag nach einem Upload-Fehler.
async function fetchRetry(url, opts) {
  for (let attempt = 1; ; attempt++) {
    const res = await fetch(url, opts).catch(e => ({ ok: false, status: 599, json: async () => ({ error: e.message }) }));
    if (res.ok || (res.status < 500 && res.status !== 429) || attempt === 6) return res;
    await new Promise(r => setTimeout(r, attempt * 10000));
  }
}

const token = await accessToken();
const google = new Map(); // id -> { filename, mimeType, creationTime }
let pageToken = '', pages = 0;
do {
  const res = await fetchRetry('https://photoslibrary.googleapis.com/v1/mediaItems?' + new URLSearchParams({ pageSize: '100', ...(pageToken && { pageToken }) }), { headers: { Authorization: `Bearer ${token}` } });
  const json = await res.json();
  if (!res.ok) throw new Error(`Auflisten: ${res.status} ${JSON.stringify(json)}`);
  for (const m of json.mediaItems ?? []) google.set(m.id, { filename: m.filename, mimeType: m.mimeType, creationTime: m.mediaMetadata?.creationTime });
  pageToken = json.nextPageToken ?? '';
  pages++;
} while (pageToken);

// Protokoll: letzte Zeile je Fassung gilt
const latest = new Map();
for (const line of readFileSync(join(here, 'state', 'done.jsonl'), 'utf8').split('\n').filter(Boolean)) { const r = JSON.parse(line); latest.set(r.uuid, r); }
const logged = [...latest.values()].filter(r => r.id);
const apple = new Map(JSON.parse(readFileSync(join(here, 'state', 'library.json'), 'utf8')).map(p => [p.uuid, p]));

// Googles Liste lässt vereinzelt vorhandene Einträge aus. Deshalb jeden vermeintlich fehlenden einzeln abfragen;
// nur was auch dann nicht gefunden wird, gilt als fehlend.
const missing = [];                                                           // im Protokoll, bei Google nicht (mehr)
let nurInListeFehlend = 0;
for (const r of logged.filter(r => !google.has(r.id))) {
  const res = await fetchRetry('https://photoslibrary.googleapis.com/v1/mediaItems/' + r.id, { headers: { Authorization: `Bearer ${token}` } });
  if (res.ok) { const m = await res.json(); google.set(m.id, { filename: m.filename, mimeType: m.mimeType, creationTime: m.mediaMetadata?.creationTime }); nurInListeFehlend++; }
  else missing.push(r);
}
const loggedIds = new Set(logged.map(r => r.id));
const extra = [...google].filter(([id]) => !loggedIds.has(id)).map(([id, g]) => ({ id, ...g })); // bei Google, nicht im Protokoll
const dateOff = [];
for (const r of logged) {
  const g = google.get(r.id), p = apple.get(r.uuid.replace(/-original$/, ''));
  if (!g?.creationTime || !p?.date) continue;
  const diff = Math.abs(new Date(g.creationTime) - new Date(p.date)) / 1000;
  if (diff > 2) dateOff.push({ fileName: r.fileName, apple: p.date, google: g.creationTime, sekunden: Math.round(diff) });
}
const byDiff = {};
for (const d of dateOff) { const k = d.sekunden % 3600 === 0 ? `${d.sekunden / 3600} h` : 'sonstige'; byDiff[k] = (byDiff[k] ?? 0) + 1; }
const out = { zeit: new Date().toISOString(), anfragen: pages, beiGoogle: google.size, imProtokoll: logged.length, fehltBeiGoogle: missing.length, nurInListeFehlend, nichtImProtokoll: extra.length, datumAbweichend: dateOff.length, abweichungNachGroesse: byDiff };
writeFileSync(join(here, 'state', 'audit.json'), JSON.stringify({ ...out, missing, extra, dateOff }, null, 1));
console.log(JSON.stringify(out, null, 1));
for (const m of missing.slice(0, 5)) console.log('  fehlt bei Google:', m.fileName);
for (const e of extra.slice(0, 5)) console.log('  nicht im Protokoll:', e.filename, e.creationTime);
for (const d of dateOff.slice(0, 5)) console.log('  Datum weicht ab:', d.fileName, d.apple, '≠', d.google);
