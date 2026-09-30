#!/usr/bin/env node
// core_plan.mjs — ams package DATA, never imported by src/ams (PLAN-core §4.3).
//
//   node core_plan.mjs <tree> <outDir> <pluginId>...
//
// Run with the staged tree's own node (and its node_modules installed). For every plugin id it
// builds <tree>/plugins/<id> through the tree's own scripts/build-artifact.mjs into <outDir> and
// prints exactly one JSON line on stdout:
//
//   {"pluginId","dir","artifactId","contentKey","path","commit","dirty"}   or   {"pluginId","error"}
//
// contentKey is computeArtifactId() minus buildInfo:
//   sha256(bundle ‖ canonical(manifest) ‖ docs ‖ sources)
// buildInfo carries the commit, which changes on every commit, so artifactId cannot tell ams
// whether a plugin's *content* changed; contentKey can. It must stay byte-for-byte the formula in
// build-artifact.mjs computeArtifactId() with the fifth part dropped: same parts, same order,
// same "\0" separators, same canonical().
//
// stdout is a protocol: anything a plugin bundle prints while its manifest is probed is moved to
// stderr, so a stray console.log can never corrupt a result line.
import { createHash } from 'node:crypto';
import { stat } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const PLUGIN_ID = /^[a-z][a-z0-9-]{0,62}$/;

const out = line => process.stdout.write(JSON.stringify(line) + '\n');
for (const name of ['log', 'info', 'debug', 'warn']) {
  console[name] = (...parts) => process.stderr.write(parts.map(String).join(' ') + '\n');
}

const [treeArg, outDirArg, ...ids] = process.argv.slice(2);
if (!treeArg || !outDirArg || ids.length === 0) {
  process.stderr.write('usage: core_plan.mjs <tree> <outDir> <pluginId>...\n');
  process.exit(2);
}
const tree = resolve(treeArg);
const outDir = resolve(outDirArg);

let builder;
try {
  builder = await import(pathToFileURL(join(tree, 'scripts', 'build-artifact.mjs')).href);
} catch (error) {
  process.stderr.write(`core_plan: cannot import ${tree}/scripts/build-artifact.mjs: ${error?.message ?? error}\n`);
  process.exit(3);
}
const { buildArtifact, canonical } = builder;
if (typeof buildArtifact !== 'function' || typeof canonical !== 'function') {
  process.stderr.write('core_plan: build-artifact.mjs no longer exports buildArtifact/canonical\n');
  process.exit(3);
}

/** computeArtifactId() without the buildInfo part. */
function contentKey(file) {
  const hash = createHash('sha256');
  [file.bundle, canonical(file.manifest), file.docs, file.sources].forEach((part, index) => {
    if (index) hash.update('\0');
    hash.update(part);
  });
  return hash.digest('hex');
}

for (const pluginId of ids) {
  if (!PLUGIN_ID.test(pluginId)) { out({ pluginId, error: 'invalid plugin id' }); continue; }
  const dir = join(tree, 'plugins', pluginId);
  try {
    if (!(await stat(dir)).isDirectory()) throw new Error(`${dir} is not a directory`);
    const built = await buildArtifact(dir, { outDir });
    if (built.pluginId !== pluginId) {
      out({ pluginId, error: `manifest pluginId ${JSON.stringify(built.pluginId)} does not match directory ${pluginId}` });
      continue;
    }
    out({ pluginId, dir, artifactId: built.artifactId, contentKey: contentKey(built.file), path: built.path,
      commit: built.commit, dirty: built.dirty });
  } catch (error) {
    out({ pluginId, error: String(error?.message ?? error).slice(0, 2000) });
  }
}
