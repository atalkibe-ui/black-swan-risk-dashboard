import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { readFile, rename, writeFile, mkdir, stat } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const serverDir = fileURLToPath(new URL('.', import.meta.url));
const appRoot = resolve(serverDir, '..');
const dashboardPath = resolve(process.env.DASHBOARD_PATH || resolve(appRoot, 'dashboard/black-swan-dashboard.html'));
const refreshEnabled = enabled(process.env.DATA_REFRESH_ENABLED, false);
const refreshScriptPath = resolve(process.env.DATA_REFRESH_SCRIPT || resolve(appRoot, 'deployment/refresh-data.sh'));
const refreshRoot = resolve(process.env.DATA_REFRESH_ROOT || appRoot);
const refreshStatePath = resolve(process.env.DATA_REFRESH_STATE_FILE || resolve(serverDir, 'data/data-refresh.json'));
const host = process.env.HOST || '127.0.0.1';
const port = integer(process.env.PORT, 8080, 1, 65535);
const refreshTimeoutMs = integer(process.env.DATA_REFRESH_TIMEOUT_MS, 15 * 60_000, 10_000, 60 * 60_000);
let refreshJob = null;

function integer(value, fallback, min, max) {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isFinite(parsed) ? Math.min(max, Math.max(min, parsed)) : fallback;
}

function enabled(value, fallback = false) {
  if (value === undefined) return fallback;
  return !['0', 'false', 'no', 'off'].includes(String(value).toLowerCase());
}

function json(response, status, body) {
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  });
  response.end(body === undefined ? '' : JSON.stringify(body));
}

function securityHeaders(contentType) {
  return {
    'Content-Type': contentType,
    'Cache-Control': 'no-cache',
    'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
    'Referrer-Policy': 'same-origin',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'SAMEORIGIN',
  };
}

async function writeJson(path, value) {
  const directory = resolve(path, '..');
  const temporaryPath = `${path}.${process.pid}.tmp`;
  await mkdir(directory, { recursive: true });
  await writeFile(temporaryPath, JSON.stringify(value), { encoding: 'utf8', mode: 0o600 });
  await rename(temporaryPath, path);
}

async function dataFreshness() {
  const definitions = [
    ['economic', resolve(refreshRoot, 'research/economic-index-history.json'), 'records'],
    ['political', resolve(refreshRoot, 'research/political-index-history.json'), 'snapshots'],
    ['naturalBiological', resolve(refreshRoot, 'research/natural-biological-history.json'), 'records'],
  ];
  const sources = {};
  for (const [name, path, key] of definitions) {
    try {
      const payload = JSON.parse(await readFile(path, 'utf8'));
      const rows = payload[key];
      sources[name] = Array.isArray(rows) && rows.length ? String(rows.at(-1).date || '') : '';
    } catch {
      sources[name] = '';
    }
  }
  const dates = Object.values(sources).filter((date) => /^\d{4}-\d{2}-\d{2}$/.test(date)).sort();
  let bloombergModifiedAt = null;
  try {
    const workbook = resolve(process.env.BLOOMBERG_WORKBOOK || resolve(refreshRoot, 'research/Blackswan.xlsx'));
    bloombergModifiedAt = (await stat(workbook)).mtime.toISOString();
  } catch {}
  return {
    latestDataDate: dates.length === definitions.length ? dates[0] : null,
    sources,
    bloombergModifiedAt,
  };
}

async function getRefreshState() {
  let saved = { status: 'idle', message: 'Обновление еще не запускалось.' };
  try {
    saved = JSON.parse(await readFile(refreshStatePath, 'utf8'));
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  if (saved.status === 'running' && !refreshJob) {
    saved = { ...saved, status: 'failed', finishedAt: new Date().toISOString(), message: 'Обновление было прервано перезапуском сервера.' };
    await writeJson(refreshStatePath, saved);
  }
  return { enabled: refreshEnabled, ...saved, ...(await dataFreshness()) };
}

async function startDataRefresh() {
  if (!refreshEnabled) {
    const error = new Error('Обновление данных не настроено на этом сервере.');
    error.status = 503;
    throw error;
  }
  if (refreshJob) {
    const error = new Error('Обновление данных уже выполняется.');
    error.status = 409;
    throw error;
  }
  await stat(refreshScriptPath);
  const startedAt = new Date().toISOString();
  const running = { status: 'running', startedAt, finishedAt: null, message: 'Обновляются открытые источники и данные Bloomberg.' };
  await writeJson(refreshStatePath, running);

  const child = spawn(refreshScriptPath, [refreshRoot], {
    cwd: refreshRoot,
    env: process.env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  refreshJob = child;
  let output = '';
  const collect = (chunk) => {
    output = `${output}${chunk}`.slice(-24_000);
  };
  child.stdout.on('data', collect);
  child.stderr.on('data', collect);
  const timer = setTimeout(() => child.kill('SIGTERM'), refreshTimeoutMs);
  timer.unref();

  child.on('error', async (error) => {
    clearTimeout(timer);
    refreshJob = null;
    console.error('Data refresh could not start:', error);
    await writeJson(refreshStatePath, { ...running, status: 'failed', finishedAt: new Date().toISOString(), message: 'Не удалось запустить обновление данных.' }).catch(console.error);
  });
  child.on('close', async (code, signal) => {
    clearTimeout(timer);
    refreshJob = null;
    const succeeded = code === 0;
    if (!succeeded) console.error(`Data refresh failed (code=${code}, signal=${signal || 'none'}):\n${output}`);
    await writeJson(refreshStatePath, {
      ...running,
      status: succeeded ? 'succeeded' : 'failed',
      finishedAt: new Date().toISOString(),
      message: succeeded ? 'Данные успешно обновлены.' : 'Обновление завершилось с ошибкой. Подробности находятся в журнале сервера.',
    }).catch(console.error);
  });
  return { enabled: true, ...running, ...(await dataFreshness()) };
}

async function readiness() {
  const checks = { dashboard: false };
  try {
    await readFile(dashboardPath);
    checks.dashboard = true;
  } catch {}
  return { ready: Object.values(checks).every(Boolean), checks };
}

async function handle(request, response) {
  const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);
  if (request.method === 'GET' && url.pathname === '/health') {
    return json(response, 200, { status: 'ok' });
  }
  if (request.method === 'GET' && url.pathname === '/ready') {
    const state = await readiness();
    return json(response, state.ready ? 200 : 503, state);
  }
  if (request.method === 'GET' && url.pathname === '/api/data-refresh') {
    return json(response, 200, await getRefreshState());
  }
  if (request.method === 'POST' && url.pathname === '/api/data-refresh') {
    if (request.headers['x-requested-with'] !== 'black-swan-dashboard') {
      return json(response, 403, { error: 'Запрос на обновление не прошел проверку источника.' });
    }
    return json(response, 202, await startDataRefresh());
  }
  if (request.method === 'GET' && ['/', '/index.html'].includes(url.pathname)) {
    const html = await readFile(dashboardPath);
    response.writeHead(200, securityHeaders('text/html; charset=utf-8'));
    return response.end(html);
  }
  if (request.method === 'GET' && url.pathname === '/favicon.ico') {
    response.writeHead(204, securityHeaders('image/x-icon'));
    return response.end();
  }
  return json(response, 404, { error: 'Маршрут не найден.' });
}

const server = createServer((request, response) => {
  handle(request, response).catch((error) => {
    console.error(error.status ? error.message : error);
    return json(response, error.status || 500, { error: error.status ? error.message : 'Внутренняя ошибка сервера.' });
  });
});

server.listen(port, host, () => {
  console.log(`Black Swan dashboard server listening on http://${host}:${port}`);
});

function shutdown(signal) {
  console.log(`${signal} received; stopping server.`);
  server.close((error) => process.exit(error ? 1 : 0));
  setTimeout(() => process.exit(1), 10_000).unref();
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
