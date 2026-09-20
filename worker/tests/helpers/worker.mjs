import { build } from 'esbuild';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export async function loadWorker(exports = []) {
  const contents = readFileSync(new URL('../../src/index.ts', import.meta.url), 'utf8') +
    (exports.length ? `\nexport { ${exports.join(', ')} };` : '');
  const { outputFiles } = await build({
    stdin: { contents, loader: 'ts', resolveDir: fileURLToPath(new URL('../../src/', import.meta.url)) },
    bundle: true, write: false, format: 'esm', platform: 'node', target: 'es2022',
    external: [import.meta.resolve('@google/genai')],
    plugins: [{ name: 'genai', setup(build) {
      build.onResolve({ filter: /^@google\/genai$/ }, () => ({ path: import.meta.resolve('@google/genai'), external: true }));
    }}],
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);
}
