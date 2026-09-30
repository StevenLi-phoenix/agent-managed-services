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
//
// buildArtifact evaluates each bundle's module scope in THIS process. core installs handlers for
// unhandled rejections and uncaught exceptions (packages/core/main.ts), so a bundle that rejects a
// promise or throws from a timer runs fine there; here, without handlers, Node would exit and every
// plugin's plan would be lost. So the handlers below pin such an error on the plugin being built
// (or, if it fires between builds, on the one just built -- a later line for the same pluginId
// overrides its success line), and the run ends with process.exit so a module-scope setInterval
// cannot keep it alive.
import { createHash } from 'node:crypto';
import { stat } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const PLUGIN_ID = /^[a-z][a-z0-9-]{0,62}$/;

const out = line => process.stdout.write(JSON.stringify(line) + '\n');

let current = null;
let previous = null;
const strayFailed = new Set();
function stray(kind, error) {
  const pluginId = current ?? previous;
  const message = `${kind} while building: ${String(error?.stack ?? error?.message ?? error)}`.slice(0, 2000);
  process.stderr.write(`core_plan: ${pluginId ?? '(no plugin)'}: ${message}\n`);
  if (pluginId && !strayFailed.has(pluginId)) {
    strayFailed.add(pluginId);
    out({ pluginId, error: message });
  }
}
process.on('unhandledRejection', error => stray('unhandled rejection', error));
process.on('uncaughtException', error => stray('uncaught exception', error));
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
  current = pluginId;
  try {
    if (!(await stat(dir)).isDirectory()) throw new Error(`${dir} is not a directory`);
    const built = await buildArtifact(dir, { outDir });
    if (strayFailed.has(pluginId)) continue;  // its error line is already out
    if (built.pluginId !== pluginId) {
      out({ pluginId, error: `manifest pluginId ${JSON.stringify(built.pluginId)} does not match directory ${pluginId}` });
      continue;
    }
    out({ pluginId, dir, artifactId: built.artifactId, contentKey: contentKey(built.file), path: built.path,
      commit: built.commit, dirty: built.dirty });
  } catch (error) {
    if (!strayFailed.has(pluginId)) out({ pluginId, error: String(error?.message ?? error).slice(0, 2000) });
  } finally {
    previous = pluginId;
    current = null;
  }
}
// Give a late rejection from the last bundle one turn to land, flush stdout, then exit: a
// module-scope timer or open handle in some bundle must not hold the planner open.
await new Promise(resolveTurn => setTimeout(resolveTurn, 50));
process.stdout.write('', () => process.exit(0));
