const $ = (id) => document.getElementById(id);
const storageKey = 'cyberrange.player-token';
let token = sessionStorage.getItem(storageKey);
let busy = false;
let activeMatch = null;
let clock = null;
let reportMatch = null;
let reportLoaded = false;
let eventCursor = 0;

function duration(seconds) {
  const value = Math.floor(Math.abs(seconds));
  const parts = [Math.floor(value / 60) % 60, value % 60];
  if (value >= 3600) parts.unshift(Math.floor(value / 3600));
  return (seconds < 0 ? '-' : '') + parts.map(n => String(n).padStart(2, '0')).join(':');
}

function listText(id, lines) {
  $(id).replaceChildren(...lines.map(line => {
    const item = document.createElement('li');
    item.textContent = line;
    return item;
  }));
}

async function loadReport(match) {
  if (!reportLoaded) {
    const report = await api(`/matches/${match.id}/report`);
    $('report-title').textContent = `MATCH #${report.match_id}`;
    $('report-winner').textContent = report.winner;
    $('report-duration').textContent = duration(report.duration_seconds);
    listText('report-red', report.notes.RED);
    listText('report-blue', report.notes.BLUE);
    listText('report-service', Object.entries(report.services).map(([name, data]) =>
      `${name}: uptime ${duration(data.uptime_seconds)}; downtime ${duration(data.downtime_seconds)}; ` +
      `самый длинный простой ${duration(data.longest_downtime_seconds)}; ` +
      `неизвестно ${duration(data.unknown_seconds)}; uptime ${data.uptime_percent === null ? '—' : data.uptime_percent.toFixed(2) + '%'}.`));
    if (!Object.keys(report.services).length) listText('report-service', ['Нет данных о состоянии сервисов.']);
    reportLoaded = true;
    $('report-data').hidden = false;
  }
  const labels = {
    MATCH_CREATED: 'Матч создан', TARGET_STARTED: 'Target запущен', MATCH_STARTED: 'Матч начался',
    TARGET_HEALTHY: 'Target прошёл health-check', BLUE_FIRST_LOGIN: 'SSH-вход BLUE (по логу target)',
    SERVICE_UP: 'Сервис доступен', SERVICE_DOWN: 'Сервис недоступен', SERVICE_RESTORED: 'Сервис восстановлен',
    EXPLOIT_AVAILABLE: 'Exploit доступен', EXPLOIT_BLOCKED: 'Exploit заблокирован', EXPLOIT_RESTORED: 'Exploit снова доступен',
    BLUE_SECURING_STARTED: 'Стабилизация началась', BLUE_SECURING_CANCELLED: 'Стабилизация отменена',
    BLUE_SECURED_TARGET: 'Стабилизация пройдена', BLUE_KEY_ISSUED: 'BLUE_KEY выдан',
    KEY_SUBMITTED_INVALID: 'Неверный ключ', KEY_SUBMITTED_RED: 'Ключ RED принят', KEY_SUBMITTED_BLUE: 'Ключ BLUE принят',
    MATCH_FINISHED: 'Матч завершён', ARENA_DESTROYED: 'Арена удалена',
  };
  while (true) {
    const page = await api(`/matches/${match.id}/events?after=${eventCursor}&limit=100`);
    for (const event of page.events) {
      const item = document.createElement('li');
      const elapsed = (Date.parse(event.timestamp) - Date.parse(match.started_at)) / 1000;
      item.textContent = `${duration(elapsed)} ${labels[event.type] || event.type}` +
        (event.metadata.service ? ` · ${event.metadata.service}` : '');
      $('report-timeline').append(item);
    }
    eventCursor = page.cursor;
    if (page.events.length < 100) break;
  }
  $('report-status').textContent = '';
}

function clearAccess() {
  $('target-info').hidden = $('admin-access').hidden = $('issued-key').hidden = true;
  for (const id of ['target-host', 'target-services', 'ssh-command', 'ssh-password', 'blue-key']) $(id).textContent = '';
}

function tick() {
  const seconds = clock ? Math.floor(clock.seconds + (performance.now() - clock.received) / 1000) : null;
  $('match-time').textContent = seconds === null ? '—' : [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
    .map(value => String(value).padStart(2, '0')).join(':');
}

function render(state) {
  const running = state?.match?.state === 'RUNNING';
  const finished = state?.match?.state === 'FINISHED';
  const nextReport = finished ? state.match.id : null;
  if (reportMatch !== nextReport) {
    reportMatch = nextReport;
    reportLoaded = false;
    eventCursor = 0;
    $('report-data').hidden = true;
    $('report-title').textContent = '';
    $('report-timeline').replaceChildren();
    $('report-status').textContent = 'Загружаем отчёт…';
  }
  $('report').hidden = !finished;
  document.body.classList.toggle('playing', running || finished);
  if (!running || activeMatch !== state.match.id) {
    clearAccess();
    $('key').value = '';
  }
  activeMatch = running ? state.match.id : null;
  $('game').hidden = !running;
  $('submit-key').disabled = !running || busy;
  $('match-result').hidden = !state?.match?.winner;
  $('match-result').textContent = state?.match?.winner ? `WINNER: ${state.match.winner}` : '';
  if (!state?.match) $('submission-result').textContent = '';
  clock = running && state.match.elapsed_seconds !== null
    ? { seconds: state.match.elapsed_seconds, received: performance.now() } : null;
  tick();
  $('register').hidden = Boolean(state);
  $('player-panel').hidden = !state;
  $('lobby-title').textContent = finished ? 'Матч завершён.' : running ? 'Матч идёт.' : state ? 'Ты в лобби.' : 'Готов к игре?';
  $('hint').textContent = state ? 'Начни поиск или дождись соперников в очереди.' : 'Выбери nickname, чтобы войти в лобби.';
  if (!state) return;
  $('player-name').textContent = state.player.nickname;
  const searching = state.status === 'SEARCHING';
  const matched = state.status === 'MATCHED';
  const match = state.match;
  $('state-label').textContent = match ? match.state : searching ? 'SEARCHING FOR MATCH' : 'READY';
  $('queue-count').textContent = `Игроков в очереди: ${state.queued_players}`;
  $('play').hidden = searching || matched;
  $('play').textContent = finished ? 'НАЧАТЬ НОВУЮ ИГРУ' : 'НАЧАТЬ ИГРУ';
  $('cancel').hidden = !searching;
  $('logout').hidden = matched;
  $('match-info').hidden = !match;
  if (match) {
    $('game-team').textContent = `${match.team} TEAM`;
    $('game-team').className = match.team.toLowerCase();
    $('match-id').textContent = `#${match.id}`;
    $('match-case').textContent = match.case_id;
    $('match-team').textContent = match.team;
    $('match-allies').textContent = match.allies.map(player => player.nickname).join(', ');
    $('hint').textContent = match.state === 'FAILED'
      ? 'Не удалось подготовить арену. Можно начать поиск снова.'
      : match.state === 'FINISHED' ? 'Матч завершён. Можно начать новую игру.'
      : match.state === 'RUNNING' ? 'Арена готова.' : 'Подготавливаем арену. Дождись запуска.';
  }
}

async function api(path, method = 'GET', data) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(path.startsWith('/') ? path : `/lobby/${path}`, {
      method, signal: controller.signal, cache: 'no-store',
      headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(data ? { 'Content-Type': 'application/json' } : {}) },
      body: data ? JSON.stringify(data) : undefined,
    });
    const result = await response.json();
    if (response.status === 401) {
      token = null;
      sessionStorage.removeItem(storageKey);
      render(null);
      throw new Error('Сессия истекла. Выбери nickname снова.');
    }
    if (!response.ok) {
      const detail = Array.isArray(result.detail) ? result.detail.map(e => e.msg).join('. ') : result.detail;
      throw new Error(detail || 'Запрос не выполнен. Попробуй ещё раз.');
    }
    return result;
  } finally {
    clearTimeout(timeout);
  }
}

async function refresh() {
  const state = await api('session');
  render(state);
  if (state.match?.state === 'FINISHED') {
    try {
      await loadReport(state.match);
    } catch (error) {
      $('report-status').textContent = 'Не удалось загрузить отчёт или события. Повторяем запрос.';
      throw error;
    }
    return;
  }
  if (!activeMatch) return;
  try {
    const access = await api(`/matches/${activeMatch}/access`);
    clearAccess();
    $('target-host').textContent = access.host;
    for (const service of access.services || []) {
      const item = document.createElement('li');
      item.textContent = `${service.name}: ${service.host}:${service.port} (${service.protocol})`;
      $('target-services').append(item);
    }
    $('target-info').hidden = false;
    if (state.match.team === 'BLUE' && access.password) {
      $('ssh-command').textContent = `ssh -p ${access.port} ${access.username}@${access.host}`;
      $('ssh-password').textContent = access.password;
      $('admin-access').hidden = false;
      if (access.blue_key) {
        $('blue-key').textContent = access.blue_key;
        $('issued-key').hidden = false;
      }
    }
    $('access-status').textContent = '';
  } catch (error) {
    clearAccess();
    $('access-status').textContent = 'Доступ к арене временно недоступен. Повторяем запрос.';
    throw error;
  }
}

async function action(work) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(button => { button.disabled = true; });
  $('error').hidden = true;
  try {
    await work();
  } catch (error) {
    clearAccess();
    $('error').textContent = error.name === 'AbortError' || error instanceof TypeError
      ? 'Нет связи с сервером. Попробуй ещё раз.' : error.message;
    $('error').hidden = false;
  } finally {
    busy = false;
    document.querySelectorAll('button:not(#submit-key)').forEach(button => { button.disabled = false; });
    $('submit-key').disabled = !activeMatch;
  }
}

$('register').addEventListener('submit', event => {
  event.preventDefault();
  action(async () => {
    const state = await api('session', 'POST', { nickname: $('nickname').value });
    token = state.token;
    sessionStorage.setItem(storageKey, token);
    render(state);
    $('play').focus();
  });
});
$('play').addEventListener('click', () => action(async () => render(await api('queue', 'POST'))));
$('cancel').addEventListener('click', () => action(async () => render(await api('queue', 'DELETE'))));
$('logout').addEventListener('click', () => action(async () => {
  await api('session', 'DELETE');
  token = null;
  sessionStorage.removeItem(storageKey);
  render(null);
  $('nickname').focus();
}));
render(null);
$('key-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!activeMatch) return;
  action(async () => {
    const result = await api(`/matches/${activeMatch}/submit`, 'POST', { key: $('key').value });
    $('key').value = '';
    $('submission-result').textContent = {
      INVALID: 'Неверный ключ. Матч продолжается.',
      RED_WIN: 'RED WIN', BLUE_WIN: 'BLUE WIN',
      MATCH_ALREADY_FINISHED: 'Матч уже завершён.',
    }[result.result];
    await refresh();
  });
});
setInterval(tick, 1000);
if (token) action(refresh);
setInterval(() => {
  if (token) action(refresh);
}, 5000);
