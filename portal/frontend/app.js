const $ = (id) => document.getElementById(id);
const storageKey = 'cyberrange.player-token';
let token = sessionStorage.getItem(storageKey);
let busy = false;

function render(state) {
  $('register').hidden = Boolean(state);
  $('player-panel').hidden = !state;
  $('lobby-title').textContent = state ? 'Ты в лобби.' : 'Готов к игре?';
  $('hint').textContent = state ? 'Начни поиск или дождись соперников в очереди.' : 'Выбери nickname, чтобы войти в лобби.';
  if (!state) return;
  $('player-name').textContent = state.player.nickname;
  const searching = state.status === 'SEARCHING';
  $('state-label').textContent = searching ? 'SEARCHING FOR MATCH' : 'READY';
  $('queue-count').textContent = `Игроков в очереди: ${state.queued_players}`;
  $('play').hidden = searching;
  $('cancel').hidden = !searching;
}

async function api(path, method = 'GET', data) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(`/lobby/${path}`, {
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

async function action(work) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(button => { button.disabled = true; });
  $('error').hidden = true;
  try {
    await work();
  } catch (error) {
    $('error').textContent = error.name === 'AbortError' || error instanceof TypeError
      ? 'Нет связи с сервером. Попробуй ещё раз.' : error.message;
    $('error').hidden = false;
  } finally {
    busy = false;
    document.querySelectorAll('button').forEach(button => { button.disabled = false; });
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
if (token) action(async () => render(await api('session')));
setInterval(() => {
  if (token) action(async () => render(await api('session')));
}, 5000);
