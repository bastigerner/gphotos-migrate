// Sammelt Einträge in einem Hilfsalbum, damit bei Google EINE Mehrfachauswahl reicht – die API selbst kann weder
// Favoriten-Sterne setzen noch in den gesperrten Ordner verschieben.
// Aufruf: node album-collect.mjs --favorites|--hidden ["Albumtitel"]
//   --favorites  alle Favoriten (sichtbare Fassung)  -> im Album alle markieren, Stern setzen
//   --hidden     alle ausgeblendeten Fotos           -> im Album alle markieren, in den gesperrten Ordner verschieben
// Danach das Hilfsalbum bei Google von Hand löschen (die Fotos bleiben in der Mediathek).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { here, google, addToAlbum } from './google.mjs';

const mode = process.argv[2];
if (!['--favorites', '--hidden'].includes(mode)) { console.error('Aufruf: node album-collect.mjs --favorites|--hidden ["Albumtitel"]'); process.exit(2); }
const title = process.argv[3] ?? (mode === '--favorites' ? 'ZZ Favoriten – alle markieren, Stern setzen' : 'ZZ In gesperrten Ordner verschieben');
const latest = new Map();
for (const line of readFileSync(join(here, 'state', 'done.jsonl'), 'utf8').split('\n').filter(Boolean)) { const r = JSON.parse(line); if (r.id) latest.set(r.uuid, r); }
const ids = [...latest.values()].filter(r => mode === '--favorites' ? r.favorite && !r.uuid.endsWith('-original') : r.hidden).map(r => r.id);
if (!ids.length) { console.log('Keine passenden Einträge im Protokoll.'); process.exit(0); }
const album = await google('/albums', { album: { title } }, { write: true });
await addToAlbum(album.id, ids);
console.log(`Album "${title}": ${ids.length} Einträge | ${album.productUrl}`);
