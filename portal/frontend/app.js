const $ = (id) => document.getElementById(id);
const storageKey = 'cyberrange.player-token';
let token = sessionStorage.getItem(storageKey);
let busy = false;
let activeMatch = null;
let clock = null;

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
  document.body.classList.toggle('playing', running);
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
  $('lobby-title').textContent = running ? 'Матч идёт.' : state ? 'Ты в лобби.' : 'Готов к игре?';
  $('hint').textContent = state ? 'Начни поиск или дождись соперников в очереди.' : 'Выбери nickname, чтобы войти в лобби.';
  if (!state) return;
  $('player-name').textContent = state.player.nickname;
  const searching = state.status === 'SEARCHING';
  const matched = state.status === 'MATCHED';
  const match = state.match;
  $('state-label').textContent = match ? match.state : searching ? 'SEARCHING FOR MATCH' : 'READY';
  $('queue-count').textContent = `Игроков в очереди: ${state.queued_players}`;
  $('play').hidden = searching || matched;
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
