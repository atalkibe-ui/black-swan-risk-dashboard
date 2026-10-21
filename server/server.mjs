import { createServer } from 'node:http';
import { readFile, rename, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const serverDir = fileURLToPath(new URL('.', import.meta.url));
const dashboardPath = resolve(serverDir, '../dashboard/black-swan-dashboard.html');
const promptPath = resolve(process.env.AI_PROMPT_FILE || resolve(serverDir, 'prompts/analysis-prompt.txt'));
const cachePath = resolve(process.env.AI_CACHE_FILE || resolve(serverDir, 'data/latest-analysis.json'));
const host = process.env.HOST || '127.0.0.1';
const port = integer(process.env.PORT, 8080, 1, 65535);
const maxBodyBytes = integer(process.env.MAX_REQUEST_BYTES, 64 * 1024, 1024, 1024 * 1024);
const timeoutMs = integer(process.env.AI_TIMEOUT_MS, 60_000, 1000, 300_000);

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

async function readJsonBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBodyBytes) {
      const error = new Error('Размер запроса превышает допустимый лимит.');
      error.status = 413;
      throw error;
    }
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    const error = new Error('Некорректный JSON в запросе.');
    error.status = 400;
    throw error;
  }
}

function validateContext(context) {
  if (!context || !Array.isArray(context.indices) || context.indices.length !== 3) {
    const error = new Error('Не передан полный срез трех индексов.');
    error.status = 400;
    throw error;
  }
  const normalized = {
    dashboardUpdated: String(context.dashboardUpdated || '').slice(0, 160),
    indices: context.indices.map((index) => ({
      name: String(index.name || '').slice(0, 120),
      date: String(index.date || '').slice(0, 120),
      composite: finiteScore(index.composite),
      band: String(index.band || '').slice(0, 30),
      redThreshold: finiteScore(index.redThreshold),
      components: Array.isArray(index.components) ? index.components.slice(0, 12).map((component) => ({
        name: String(component.name || '').slice(0, 160),
        value: finiteScore(component.value),
      })) : [],
    })),
  };
  if (normalized.indices.some((index) => !index.name || index.components.length === 0)) {
    const error = new Error('Срез индексов содержит неполные данные.');
    error.status = 400;
    throw error;
  }
  return normalized;
}

function finiteScore(value) {
  const score = Number(value);
  if (!Number.isFinite(score) || score < 0 || score > 100) {
    const error = new Error('Значение индекса должно находиться в диапазоне 0-100.');
    error.status = 400;
    throw error;
  }
  return Math.round(score * 10) / 10;
}

async function readPrompt() {
  let prompt;
  try {
    prompt = (await readFile(promptPath, 'utf8')).trim();
  } catch (error) {
    if (error.code === 'ENOENT') {
      const configurationError = new Error('Промпт для аналитического комментария еще не настроен на сервере.');
      configurationError.status = 503;
      throw configurationError;
    }
    throw error;
  }
  if (!prompt) {
    const error = new Error('Промпт для аналитического комментария пуст.');
    error.status = 503;
    throw error;
  }
  return prompt;
}

function providerConfiguration() {
  const mode = process.env.AI_API_MODE || 'responses';
  if (!['responses', 'chat-completions'].includes(mode)) {
    const error = new Error('AI_API_MODE должен быть responses или chat-completions.');
    error.status = 503;
    throw error;
  }
  const baseUrl = (process.env.OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\/$/, '');
  const model = process.env.OPENAI_MODEL || '';
  if (!model) {
    const error = new Error('На сервере не задана переменная OPENAI_MODEL.');
    error.status = 503;
    throw error;
  }
  return {
    mode,
    baseUrl,
    model,
    apiKey: process.env.OPENAI_API_KEY || '',
    webSearch: enabled(process.env.AI_ENABLE_WEB_SEARCH, true),
  };
}

function modelRequest(configuration, prompt, context) {
  const input = `Дата запуска анализа: ${new Date().toISOString().slice(0, 10)}.\nПриложенный пользователем срез данных дашборда в JSON:\n${JSON.stringify(context, null, 2)}`;
  if (configuration.mode === 'chat-completions') {
    const body = { model: configuration.model, messages: [{ role: 'system', content: prompt }, { role: 'user', content: input }] };
    if (configuration.webSearch) body.web_search_options = { search_context_size: 'high' };
    return {
      url: `${configuration.baseUrl}/chat/completions`,
      body,
    };
  }
  const body = { model: configuration.model, instructions: prompt, input };
  if (configuration.webSearch) body.tools = [{ type: 'web_search', search_context_size: 'high' }];
  return {
    url: `${configuration.baseUrl}/responses`,
    body,
  };
}

function extractAnswer(payload, mode) {
  if (mode === 'chat-completions') return payload?.choices?.[0]?.message?.content?.trim() || '';
  if (typeof payload?.output_text === 'string') return payload.output_text.trim();
  return (payload?.output || []).flatMap((item) => item?.content || []).filter((item) => item?.type === 'output_text').map((item) => item.text).join('\n').trim();
}

async function requestAnalysis(context) {
  const prompt = await readPrompt();
  const configuration = providerConfiguration();
  const request = modelRequest(configuration, prompt, context);
  const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (configuration.apiKey) headers.Authorization = `Bearer ${configuration.apiKey}`;
  const response = await fetch(request.url, {
    method: 'POST',
    headers,
    body: JSON.stringify(request.body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    console.error(`AI provider returned HTTP ${response.status}.`);
    const error = new Error('Сервис модели не смог сформировать ответ.');
    error.status = 502;
    throw error;
  }
  const answer = extractAnswer(payload, configuration.mode);
  if (!answer) {
    const error = new Error('Сервис модели вернул пустой ответ.');
    error.status = 502;
    throw error;
  }
  return { answer, generatedAt: new Date().toISOString(), model: configuration.model, cached: false };
}

async function saveLatest(result) {
  const directory = resolve(cachePath, '..');
  const temporaryPath = `${cachePath}.${process.pid}.tmp`;
  await mkdir(directory, { recursive: true });
  await writeFile(temporaryPath, JSON.stringify(result), { encoding: 'utf8', mode: 0o600 });
  await rename(temporaryPath, cachePath);
}

async function getLatest() {
  try {
    return JSON.parse(await readFile(cachePath, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

async function handle(request, response) {
  const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);
  if (request.method === 'GET' && url.pathname === '/health') {
    return json(response, 200, { status: 'ok' });
  }
  if (request.method === 'GET' && url.pathname === '/api/ai-analysis') {
    const latest = await getLatest();
    return latest ? json(response, 200, { ...latest, cached: true }) : json(response, 204);
  }
  if (request.method === 'POST' && url.pathname === '/api/ai-analysis') {
    if (!(request.headers['content-type'] || '').toLowerCase().startsWith('application/json')) {
      return json(response, 415, { error: 'Ожидается Content-Type application/json.' });
    }
    const body = await readJsonBody(request);
    const context = validateContext(body.context);
    const result = await requestAnalysis(context);
    await saveLatest(result);
    return json(response, 200, result);
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
    if (error.name === 'TimeoutError') {
      return json(response, 504, { error: 'Превышено время ожидания ответа модели.' });
    }
    console.error(error.status ? error.message : error);
    return json(response, error.status || 500, { error: error.status ? error.message : 'Внутренняя ошибка сервера.' });
  });
});

server.listen(port, host, () => {
  console.log(`Black Swan dashboard server listening on http://${host}:${port}`);
  console.log(`AI prompt: ${promptPath}`);
});
