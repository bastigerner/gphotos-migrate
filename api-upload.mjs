// Lädt die Dateien eines Manifests über die Google Photos Library API hoch, legt Alben an
// und schreibt results.json (für Prüfung und Favoriten-Stern).
// Aufruf: node api-upload.mjs <manifest.json>
// Braucht client_secret.json (OAuth-Client "Desktop-App") neben diesem Skript; Token in token.json.
import { readFileSync, writeFileSync, existsSync, statSync, createReadStream } from 'node:fs';
import { Readable } from 'node:stream';
import { createServer } from 'node:http';
import { execFile, execFileSync } from 'node:child_process';
import { extname, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { albumBook, knownAlbums, rememberAlbum } from './albums.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const TOKEN = join(here, 'token.json');
const SCOPE = [
  'https://www.googleapis.com/auth/photoslibrary.appendonly',
  'https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata',
].join(' ');
const MIME = {
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.heic': 'image/heic', '.png': 'image/png', '.gif': 'image/gif',
  '.tif': 'image/tiff', '.tiff': 'image/tiff', '.webp': 'image/webp', '.avif': 'image/avif', '.dng': 'image/x-adobe-dng',
  '.mov': 'video/quicktime', '.mp4': 'video/mp4', '.m4v': 'video/mp4',
};
const API = 'https://photoslibrary.googleapis.com/v1';

const secret = JSON.parse(readFileSync(join(here, 'client_secret.json'), 'utf8'));
const { client_id, client_secret } = secret.installed ?? secret.web;

async function tokenRequest(params) {
  // Googles Anmeldedienst antwortet gelegentlich mit einem 5xx ("internal_failure"); die Anfrage legt nichts an,
  // ist also gefahrlos wiederholbar. Ohne Wiederholung hielte so ein Aussetzer den Lauf minutenlang auf.
  for (let attempt = 1; ; attempt++) {
    const res = await fetch('https://oauth2.googleapis.com/token', {
      method: 'POST',
      body: new URLSearchParams({ client_id, client_secret, ...params }),
    });
    const json = await res.json().catch(() => ({}));
    if (res.ok) return json;
    if (res.status < 500 || attempt === 5) throw new Error('Token-Anfrage fehlgeschlagen: ' + JSON.stringify(json));
    console.log(`  Token-Anfrage: ${res.status}, Versuch ${attempt} von 5 – warte ${attempt * 10} s`);
    await new Promise(r => setTimeout(r, attempt * 10000));
  }
}

async function accessToken() {
  const saved = existsSync(TOKEN) ? JSON.parse(readFileSync(TOKEN, 'utf8')) : {};
  if (saved.refresh_token && saved.scope === SCOPE) {
    return (await tokenRequest({ grant_type: 'refresh_token', refresh_token: saved.refresh_token })).access_token;
  }
  const [code, redirect_uri] = await new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1');
      res.end('Freigabe erhalten, dieses Fenster kann zu.');
      server.close();
      url.searchParams.get('code') ? resolve([url.searchParams.get('code'), server.redirect]) : reject(new Error('Freigabe abgelehnt'));
    }).listen(0, '127.0.0.1', () => {
      server.redirect = `http://127.0.0.1:${server.address().port}`;
      const auth = 'https://accounts.google.com/o/oauth2/v2/auth?' + new URLSearchParams({
        client_id, redirect_uri: server.redirect, response_type: 'code', scope: SCOPE, access_type: 'offline', prompt: 'consent',
      });
      console.log('Freigabe im Browser bestätigen (Fenster öffnet sich).');
      execFile('open', [auth]);
    });
  });
  const json = await tokenRequest({ grant_type: 'authorization_code', code, redirect_uri });
  writeFileSync(TOKEN, JSON.stringify({ refresh_token: json.refresh_token, scope: SCOPE }), { mode: 0o600 });
  return json.access_token;
}

// Zugriffstoken gelten rund eine Stunde; ein Stapel mit Videos kann bei langsamer Leitung länger dauern.
// Deshalb vor jeder Anfrage prüfen und nach 45 Minuten erneuern.
let auth = { Authorization: `Bearer ${await accessToken()}` };
let tokenAt = Date.now();
async function freshAuth() {
  if (Date.now() - tokenAt > 45 * 60 * 1000) {
    auth = { Authorization: `Bearer ${await accessToken()}` };
    tokenAt = Date.now();
  }
  return auth;
}

// Google bricht einzelne Anfragen gelegentlich mit 409 ABORTED oder einem 5xx ab und verlangt dann einen neuen
// Versuch. Ohne Wiederholung hielte so ein Einzelfall den ganzen Lauf an.
// Ausnahme batchCreate: dort hat Google trotz 409 schon alle Dateien angelegt – eine Wiederholung könnte Dubletten
// erzeugen. Der Lauf hält dann an; audit.mjs zeigt die angelegten Dateien, die ins Protokoll nachzutragen sind.
async function call(path, body) {
  for (let attempt = 1; ; attempt++) {
    const res = await fetch(API + path, { method: 'POST', headers: { ...(await freshAuth()), 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const json = await res.json().catch(() => ({}));
    if (res.ok) return json;
    // 429 "concurrent write request": zu viele gleichzeitige Schreibzugriffe – nichts wurde angelegt, also gefahrlos wiederholbar.
    const busy = res.status === 429 && /concurrent|per minute/.test(json.error?.message ?? '');
    if (!busy && (path.endsWith(':batchCreate') || ![409, 500, 502, 503, 504].includes(res.status)) || attempt === 5) throw new Error(`${path}: ${res.status} ${JSON.stringify(json)}`);
    console.log(`  ${path}: ${res.status}, Versuch ${attempt} von 5 – warte ${attempt * 10} s`);
    await new Promise(r => setTimeout(r, attempt * 10000));
  }
}

const albumName = albumBook(here);
async function albumId(title) {
  let id = knownAlbums(here)[title];
  if (!id) {
    id = (await call('/albums', { album: { title } })).id;
    rememberAlbum(here, title, id);
    console.log('Album angelegt:', title);
  }
  return id;
}

const manifestPath = process.argv[2];
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const resultsPath = join(dirname(manifestPath), 'results.json');
const results = existsSync(resultsPath) ? JSON.parse(readFileSync(resultsPath, 'utf8')) : [];
const done = new Set(results.filter(r => r.id).map(r => r.uuid));

// batchCreate nimmt bis zu 50 Dateien, aber nur EIN Zielalbum je Aufruf: deshalb nach erstem Album
// sortieren und den Stapel bei Albumwechsel oder 50 Dateien abschicken. Spart Tageskontingent.
let quotaHit = false;
let pending = []; // [{ item, uploadToken }]
async function flush() {
  if (!pending.length) return;
  const batch = pending; pending = [];
  const first = batch[0].item.albums[0];
  const body = { newMediaItems: batch.map(({ item, uploadToken }) => ({ description: item.description.slice(0, 1000), simpleMediaItem: { uploadToken, fileName: item.fileName } })) };
  if (first) body.albumId = await albumId(albumName(first, batch.length));
  const created = (await call('/mediaItems:batchCreate', body)).newMediaItemResults ?? [];
  // Inhaltsgleiche Dubletten im selben Aufruf lehnt Google mit "error while trying to create" ab; im zweiten Aufruf
  // erkennt es die Dublette und liefert die Kennung der schon angelegten Datei. Nur diese Meldung wiederholen:
  // bei anderen Fehlern (z. B. Albumlimit) ist die Datei trotz Fehlermeldung angelegt.
  const rejected = new Set(created.filter(c => !c.mediaItem?.id && /error while trying to create/.test(c.status?.message ?? '')).map(c => c.uploadToken));
  if (rejected.size) {
    const again = (await call('/mediaItems:batchCreate', { ...body, newMediaItems: body.newMediaItems.filter(n => rejected.has(n.simpleMediaItem.uploadToken)) })).newMediaItemResults ?? [];
    for (const c of again) if (c.mediaItem?.id) created[created.findIndex(x => x.uploadToken === c.uploadToken)] = c;
    console.log(`  Dubletten-Nachversuch: ${again.filter(c => c.mediaItem?.id).length} von ${rejected.size} angenommen`);
  }
  // Manche Videos (beobachtet: MP4 ohne Tonspur mit Kennung "mp4v") lehnt Google mit derselben Meldung ab, nimmt sie
  // aber an, sobald sie ohne Neukodierung neu verpackt sind. Bild und Ton bleiben dabei bitgleich; das Aufnahmedatum
  // wird aus der Vorlage übernommen.
  for (const entry of batch) {
    const c = created.find(x => x.uploadToken === entry.uploadToken);
    const ext = extname(entry.item.file).toLowerCase();
    if (c?.mediaItem?.id || !/error while trying to create/.test(c?.status?.message ?? '') || !['.mp4', '.m4v', '.mov'].includes(ext)) continue;
    const packed = entry.item.file.slice(0, -ext.length) + '.neu' + ext;
    try {
      execFileSync('ffmpeg', ['-v', 'error', '-y', '-i', entry.item.file, '-c', 'copy', '-map_metadata', '0', ...(ext === '.mov' ? [] : ['-brand', 'mp42']), '-movflags', '+faststart', packed]);
      execFileSync('exiftool', ['-q', '-overwrite_original', '-tagsFromFile', entry.item.file, '-Keys:CreationDate', packed]);
      await uploadBytes({ ...entry.item, file: packed });
      const redo = pending.splice(0);
      if (!redo.length) continue;
      const again = (await call('/mediaItems:batchCreate', { ...body, newMediaItems: [{ description: entry.item.description.slice(0, 1000), simpleMediaItem: { uploadToken: redo[0].uploadToken, fileName: entry.item.fileName } }] })).newMediaItemResults ?? [];
      console.log(`  neu verpackt: ${entry.item.fileName} – ${again[0]?.mediaItem?.id ? 'angenommen' : 'weiter abgelehnt: ' + again[0]?.status?.message}`);
      if (again[0]?.mediaItem?.id) { entry.uploadToken = redo[0].uploadToken; created.push(again[0]); }
    } catch (e) { console.log(`  neu verpacken gescheitert: ${entry.item.fileName} – ${e.message.split('\n')[0]}`); }
  }
  for (const { item, uploadToken } of batch) {
    const r = created.find(c => c.uploadToken === uploadToken);
    const entry = { uuid: item.uuid, fileName: item.fileName, kind: item.kind, favorite: item.favorite, hidden: !!item.hidden, albums: item.albums, status: r?.status?.message, id: r?.mediaItem?.id, productUrl: r?.mediaItem?.productUrl };
    for (const title of item.albums.slice(1)) {
      if (entry.id) await call(`/albums/${await albumId(albumName(title, 1))}:batchAddMediaItems`, { mediaItemIds: [entry.id] });
    }
    results.push(entry);
  }
  writeFileSync(resultsPath, JSON.stringify(results, null, 1));
}

// Die Dateibytes gehen mit bis zu PARALLEL Uploads gleichzeitig hoch; das Anlegen (batchCreate) bleibt
// einzeln nacheinander, wie Google es für denselben Nutzer verlangt.
const PARALLEL = 6;
async function uploadBytes(item) {
  const mime = MIME[extname(item.file).toLowerCase()];
  if (!mime) { results.push({ uuid: item.uuid, fileName: item.fileName, error: 'unbekannter Dateityp' }); return; }
  // Als Stream senden: readFileSync scheitert bei Dateien ab 2 GiB und hält sonst jedes Video komplett im Speicher.
  const up = await fetch(API + '/uploads', {
    method: 'POST',
    headers: {
      ...(await freshAuth()), 'Content-Type': 'application/octet-stream', 'Content-Length': String(statSync(item.file).size),
      'X-Goog-Upload-Content-Type': mime, 'X-Goog-Upload-Protocol': 'raw',
    },
    body: Readable.toWeb(createReadStream(item.file)),
    duplex: 'half',
  });
  const uploadToken = await up.text();
  if (up.status === 429) { quotaHit = true; return; }
  if (!up.ok) { results.push({ uuid: item.uuid, fileName: item.fileName, error: `Upload ${up.status}: ${uploadToken}` }); return; }
  sentBytes += statSync(item.file).size;
  pending.push({ item, uploadToken });
}

let tBytes = 0, tCreate = 0, sentBytes = 0;
const todo = manifest.filter(item => !done.has(item.uuid)).sort((a, b) => (a.albums[0] ?? '').localeCompare(b.albums[0] ?? ''));
while (todo.length && !quotaHit) {
  // Gruppe = bis zu 50 Dateien mit demselben ersten Album
  const album = todo[0].albums[0] ?? '';
  const group = [];
  while (todo.length && group.length < 50 && (todo[0].albums[0] ?? '') === album) group.push(todo.shift());
  const workers = Array.from({ length: PARALLEL }, async () => {
    while (group.length && !quotaHit) {
      const item = group.shift();
      try { await uploadBytes(item); } catch (e) { results.push({ uuid: item.uuid, fileName: item.fileName, error: 'Upload abgebrochen: ' + e.message }); }
    }
  });
  const t0 = Date.now();
  await Promise.all(workers);
  const t1 = Date.now();
  await flush();
  tBytes += t1 - t0; tCreate += Date.now() - t1;
}
writeFileSync(resultsPath, JSON.stringify(results, null, 1));
const ok = results.filter(r => r.id).length;
const mbit = sentBytes * 8 / 1e6;
console.log(`  Zeiten: Dateien senden ${Math.round(tBytes / 1000)} s (${Math.round(mbit / 8)} MB, ${(mbit / (tBytes / 1000)).toFixed(1)} Mbit/s, ${PARALLEL} gleichzeitig), anlegen und Alben ${Math.round(tCreate / 1000)} s`);
console.log(`fertig: ${ok} von ${manifest.length} hochgeladen, Fehler: ${results.length - ok}`);
for (const r of results.filter(r => !r.id)) console.log('  Fehler:', r.fileName, r.error ?? r.status);
if (quotaHit) { console.log('Tageskontingent erreicht – später fortsetzen.'); process.exit(3); }
