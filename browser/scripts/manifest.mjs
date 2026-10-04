import { createHash } from 'node:crypto';
import { readFile, readdir } from 'node:fs/promises';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const files = ['package.json', 'package-lock.json', 'tsconfig.json'];
async function walk(path) {
  for (const entry of await readdir(path, { withFileTypes: true })) {
    const file = join(path, entry.name);
    if (entry.isDirectory()) await walk(file);
    else if (entry.isFile()) files.push(relative(root, file));
    else throw new Error('Release sources must be regular files');
  }
}
for (const dir of ['src', 'skill', 'scripts', 'test']) await walk(join(root, dir));
const hash = createHash('sha256');
for (const file of files.sort()) hash.update(file + '\0').update(await readFile(join(root, file))).update('\0');
console.log(JSON.stringify({ release: hash.digest('hex'), files }));
