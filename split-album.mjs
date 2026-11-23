// Verteilt die schon hochgeladenen Einträge eines übergroßen Albums auf Jahresalben ("WhatsApp 2021" …).
// Aufruf: node split-album.mjs <plan.json>   Plan: { album: "WhatsApp", alt: ["WhatsApp","WhatsApp (2)"], jahre: { "2021": [id,…] } }
// Fügt je Jahr zum Jahresalbum hinzu und nimmt die Einträge danach aus den alten Alben. Die Fotos selbst bleiben
// unberührt. Schreibt <plan>.done.json mit den verschobenen ids. Wiederholbar: Hinzufügen ist bei Google folgenlos,
// wenn der Eintrag schon im Album liegt.
import { readFileSync, writeFileSync } from 'node:fs';
import { knownAlbums, rememberAlbum } from './albums.mjs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const here = dirname(fileURLToPath(import.meta.url));
const secret = JSON.parse(readFileSync(join(here, 'client_secret.json'), 'utf8'));
const s = secret.installed ?? secret.web;
const { refresh_token } = JSON.parse(readFileSync(join(here, 'token.json'), 'utf8'));
let tokenAt = 0, access;
const post = async (path, body) => {
  if (Date.now() - tokenAt > 40 * 60 * 1000) {
    access = (await (await fetch('https://oauth2.googleapis.com/token', { method: 'POST', body: new URLSearchParams({ client_id: s.client_id, client_secret: s.client_secret, grant_type: 'refresh_token', refresh_token }) })).json()).access_token;
    tokenAt = Date.now();
  }
  // Läuft neben dem Upload: bei 429 (zu viele gleichzeitige Schreibzugriffe) warten und wiederholen, dazu bewusst gemächlich.
  for (let attempt = 1; ; attempt++) {
    const res = await fetch('https://photoslibrary.googleapis.com/v1' + path, { method: 'POST', headers: { Authorization: `Bearer ${access}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const json = await res.json().catch(() => ({}));
    if (res.status === 429 && /concurrent|per minute/.test(json.error?.message ?? '') && attempt < 20) { await new Promise(r => setTimeout(r, 15000)); continue; }
    await new Promise(r => setTimeout(r, 1200));
    return { ok: res.ok, status: res.status, json };
  }
};
const must = async (path, body) => { const r = await post(path, body); if (!r.ok) throw new Error(`${path}: ${r.status} ${JSON.stringify(r.json)}`); return r.json; };

const planPath = process.argv[2];
const plan = JSON.parse(readFileSync(planPath, 'utf8'));
const albums = knownAlbums(here);
const moved = [], notRemoved = [];
for (const [year, ids] of Object.entries(plan.jahre)) {
  const title = `${plan.album} ${year}`;
  albums[title] ??= knownAlbums(here)[title];
  if (!albums[title]) { albums[title] = (await must('/albums', { album: { title } })).id; rememberAlbum(here, title, albums[title]); console.log('Album angelegt:', title); }
  for (let i = 0; i < ids.length; i += 50) {
    const chunk = ids.slice(i, i + 50);
    await must(`/albums/${albums[title]}:batchAddMediaItems`, { mediaItemIds: chunk });
    // Aus dem alten Album nehmen: erst als Block je altem Album, bei Ablehnung einzeln.
    let left = chunk;
    for (const old of plan.alt) {
      if (!albums[old] || !left.length) continue;
      if ((await post(`/albums/${albums[old]}:batchRemoveMediaItems`, { mediaItemIds: left })).ok) { left = []; break; }
    }
    for (const id of left) {
      let ok = false;
      for (const old of plan.alt) if (albums[old] && (await post(`/albums/${albums[old]}:batchRemoveMediaItems`, { mediaItemIds: [id] })).ok) { ok = true; break; }
      if (!ok) notRemoved.push(id);
    }
    moved.push(...chunk);
    writeFileSync(planPath.replace(/\.json$/, '.done.json'), JSON.stringify({ moved, notRemoved }));
  }
  console.log(`${title}: ${ids.length} Einträge`);
}
console.log(`verschoben: ${moved.length}, nicht aus altem Album entfernt: ${notRemoved.length}`);
