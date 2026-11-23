// Führt gleichnamige Alben zusammen: Gibt es bei Google mehrere von diesem Projekt angelegte Alben mit demselben
// Titel, wandern alle Einträge in das Album, das in albums.json steht; die übrigen werden geleert.
// Aufruf: node album-merge.mjs [--apply]     ohne --apply wird nur angezeigt, was geschähe
// Die Fotos selbst bleiben unberührt. Die geleerten Alben bei Google von Hand löschen (die API kann das nicht).
import { here, google, addToAlbum } from './google.mjs';
import { knownAlbums } from './albums.mjs';

const apply = process.argv.includes('--apply');
const known = knownAlbums(here);
const all = [];
for (let page = ''; page !== undefined;) {
  const j = await google('/albums?pageSize=50&excludeNonAppCreatedData=true' + (page ? '&pageToken=' + page : ''));
  all.push(...(j.albums ?? []));
  page = j.nextPageToken;
}
let found = 0;
for (const album of all) {
  const target = known[album.title];
  if (!target || target === album.id) continue;  // nur Zweitalben eines bekannten Titels
  found++;
  const ids = [];
  for (let page; ;) {
    const j = await google('/mediaItems:search', { albumId: album.id, pageSize: 100, pageToken: page });
    ids.push(...(j.mediaItems ?? []).map(m => m.id));
    if (!(page = j.nextPageToken)) break;
  }
  console.log(`"${album.title}": Zweitalbum mit ${ids.length} Einträgen${apply ? ' – zusammengeführt' : ''}`);
  if (!apply) continue;
  await addToAlbum(target, ids);
  for (let i = 0; i < ids.length; i += 50) await google(`/albums/${album.id}:batchRemoveMediaItems`, { mediaItemIds: ids.slice(i, i + 50) }, { write: true });
}
console.log(found ? (apply ? 'Fertig. Die geleerten Alben bei Google von Hand löschen.' : 'Mit --apply ausführen.') : 'Keine gleichnamigen Alben gefunden.');
