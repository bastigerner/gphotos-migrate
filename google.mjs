// Gemeinsamer Zugang für die kleinen Hilfsskripte (album-collect.mjs, album-merge.mjs): Token holen, Anfragen stellen.
// Setzt voraus, dass api-upload.mjs (token.json) und audit.mjs (token-audit.json) schon einmal angemeldet wurden.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const here = dirname(fileURLToPath(import.meta.url));
const secret = JSON.parse(readFileSync(join(here, 'client_secret.json'), 'utf8'));
const { client_id, client_secret } = secret.installed ?? secret.web;
const tokens = {};
async function token(file) {
  if (!tokens[file]) {
    const { refresh_token } = JSON.parse(readFileSync(join(here, file), 'utf8'));
    const res = await fetch('https://oauth2.googleapis.com/token', { method: 'POST', body: new URLSearchParams({ client_id, client_secret, grant_type: 'refresh_token', refresh_token }) });
    const json = await res.json();
    if (!res.ok) throw new Error('Token-Anfrage fehlgeschlagen: ' + JSON.stringify(json));
    tokens[file] = json.access_token;
  }
  return tokens[file];
}
// Lesen läuft über token-audit.json, Schreiben über token.json (das Upload-Token darf nicht lesen).
// Die hier genutzten Aufrufe (Album anlegen ausgenommen) sind wiederholbar; bei Googles Minutenlimit (429) warten.
export async function google(path, body, { write = false } = {}) {
  for (;;) {
    const res = await fetch('https://photoslibrary.googleapis.com/v1' + path, {
      method: body ? 'POST' : 'GET',
      headers: { Authorization: 'Bearer ' + await token(write ? 'token.json' : 'token-audit.json'), 'Content-Type': 'application/json' },
      body: body && JSON.stringify(body),
    });
    if (res.status === 429) { await new Promise(r => setTimeout(r, 61000)); continue; }
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(`${path}: ${res.status} ${JSON.stringify(json)}`);
    return json;
  }
}
export async function addToAlbum(albumId, ids) {
  for (let i = 0; i < ids.length; i += 50) await google(`/albums/${albumId}:batchAddMediaItems`, { mediaItemIds: ids.slice(i, i + 50) }, { write: true });
}
