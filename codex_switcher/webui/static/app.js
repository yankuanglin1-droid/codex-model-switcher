/* codex（ChatGPT App）多平台模型切换 —— 前端逻辑 */

const TOKEN = new URLSearchParams(location.search).get('t') || '';
const api = (path, body) => {
  const url = `/api/${path}?t=${encodeURIComponent(TOKEN)}`;
  return fetch(url, body ? {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  } : undefined).then(async (response) => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  });
};

let STATE = { providers: [], presets: [], current: {} };
let SELECTED = null;
let QUERY = '';
// 两个视图：平台详情（providers）与能力查看（caps）。
// 「能力查看」单独一页 —— 能力信息挤在平台卡片里会变成一坨看不清。
let VIEW = 'providers';
// 能力查看页只看「选中的这一个模型」：几十行全摊开等于没有信息，
// 用户要的是「我用的这个模型到底能干啥」，不是一张全量对照表。
// CAPS_FOCUS = { provider, model }；CAPS_ALL 是用户主动要展开全部时才打开。
let CAPS_FOCUS = null;
let CAPS_ALL = false;
const BALANCE_TRIED = new Set();

const $ = (id) => document.getElementById(id);

function toast(message, isError = false) {
  const node = $('toast');
  node.textContent = message;
  node.classList.toggle('err', !!isError);
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 4200);
}

function shortTokens(value) {
  if (value === null || value === undefined) return '—';
  let n = Number(value);
  for (const unit of ['', 'K', 'M', 'B']) {
    if (Math.abs(n) < 1000 || unit === 'B') {
      return unit ? `${n.toFixed(1).replace('.0', '')}${unit}` : String(Math.round(n));
    }
    n /= 1000;
  }
  return String(value);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setView(view, focus) {
  VIEW = view === 'caps' ? 'caps' : 'providers';
  if (focus) {
    CAPS_FOCUS = focus;
    // 指定了具体模型就回到「只看一个」的模式，别自作主张摊开全部
    CAPS_ALL = false;
  }
  $('providers-main').hidden = VIEW !== 'providers';
  $('caps-main').hidden = VIEW !== 'caps';
  const button = $('btn-caps-page');
  if (button) {
    button.classList.toggle('primary', VIEW === 'caps');
    button.classList.toggle('ghost', VIEW !== 'caps');
  }
  if (VIEW === 'caps') { syncCapsFocus(); renderCapsPage(); }
}

// 保证 CAPS_FOCUS 指向一个真实存在的模型：模型被删了/换平台了就自动兜底，
// 否则用户会看到一张空表，还以为这个模型什么能力都没有。
function syncCapsFocus() {
  const providers = STATE.providers || [];
  if (!providers.length) { CAPS_FOCUS = null; return; }
  const held = CAPS_FOCUS && providers.find((p) => p.id === CAPS_FOCUS.provider);
  if (held && (held.models || []).indexOf(CAPS_FOCUS.model) >= 0) return;
  const current = providers.find((p) => p.is_current) || providers[0];
  const model = (current.is_current ? (STATE.current || {}).model : null)
    || current.default_model || (current.models || [])[0] || null;
  CAPS_FOCUS = model ? { provider: current.id, model: model } : null;
}

// 能力查看页顶部那一行：切模型 + 要不要展开全部
function capsFocusBar(providers) {
  const bar = el('div', 'caps-focus');
  bar.appendChild(el('span', 'caps-focus-label', t('caps.focus_label')));
  const select = el('select', 'caps-focus-select');
  for (const provider of providers) {
    const models = provider.models || [];
    if (!models.length) continue;
    const group = el('optgroup');
    group.label = provider.label || provider.id;
    for (const model of models) {
      const option = el('option', null, model);
      option.value = provider.id + '\u0001' + model;
      if (CAPS_FOCUS && CAPS_FOCUS.provider === provider.id && CAPS_FOCUS.model === model) {
        option.selected = true;
      }
      group.appendChild(option);
    }
    select.appendChild(group);
  }
  select.onchange = () => {
    const parts = select.value.split('\u0001');
    CAPS_FOCUS = { provider: parts[0], model: parts[1] };
    CAPS_ALL = false;
    renderCapsPage();
  };
  bar.appendChild(select);
  const toggle = el('button', 'btn ghost small',
    CAPS_ALL ? t('caps.only_one') : t('caps.show_all'));
  toggle.onclick = () => { CAPS_ALL = !CAPS_ALL; renderCapsPage(); };
  bar.appendChild(toggle);
  if (CAPS_ALL) bar.appendChild(el('span', 'caps-focus-note', t('caps.show_all_note')));
  else bar.appendChild(el('span', 'caps-focus-note', t('caps.only_one_note')));
  return bar;
}

async function loadState(withBalance = false) {
  STATE = await api('state' + (withBalance ? '?balance=1' : ''));
  renderHeader();
  renderThreadBanner();
  renderGuardBanner();
  renderProviders();
  // 状态回来得比用户点「能力查看」慢时，那一页会渲染成「还没有接入平台」。
  // 这里补一次，保证数据到了页面就跟着刷新。
  if (VIEW === 'caps') renderCapsPage();
  if (SELECTED) {
    const still = STATE.providers.find((p) => p.id === SELECTED);
    if (still) renderDetail(still);
    else {
      SELECTED = null;
      $('detail').innerHTML = '<div class="placeholder"></div>';
      $('detail').firstChild.textContent = t('detail.pick_provider');
    }
  } else {
    // 首次打开时自动展示当前正在使用的平台
    const current = STATE.providers.find((p) => p.is_current) || STATE.providers[0];
    if (current) {
      SELECTED = current.id;
      renderProviders();
      renderDetail(current);
    }
  }
}

function renderThreadBanner() {
  const info = STATE.threads || {};
  const banner = $('thread-banner');
  if (!banner) return;
  if (!info.mismatch) { banner.hidden = true; return; }
  banner.hidden = false;
  $('thread-banner-title').textContent = t('banner.detail', { n: info.mismatch });
  $('thread-banner-detail').textContent = t('banner.note');
}

function renderGuardBanner() {
  const banner = $('guard-banner');
  if (!banner) return;
  const g = STATE.guard;
  // 只在真有风险时出现：装得下就别打扰用户
  if (!g || !g.ratio || (g.action !== 'fork' && g.action !== 'compact-first')) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  const num = (n) => (n || 0).toLocaleString('en-US');
  const advice = g.action === 'fork' ? t('guard.action_fork') : t('guard.action_compact');
  const note = g.action === 'fork' ? t('guard.note_fork') : t('guard.note_compact');
  $('guard-banner-title').textContent = t('guard.title');
  $('guard-banner-detail').textContent = t('guard.detail', {
    tokens: num(g.tokens), window: num(g.window),
    percent: Math.round(g.ratio * 100), advice: advice,
  }) + ' ' + note;
  // 别只把问题丢回给用户：后端已经算好「这个会话还装得下谁」，
  // 这里给一个一键切换的出口，点了之后 switch_to 会连任务一起搬。
  const actions = $('guard-banner-actions');
  if (!actions) return;
  actions.innerHTML = '';
  const current = STATE.current || {};
  const best = (g.alternatives || []).find((item) =>
    !(item.provider === current.model_provider && item.model === g.model));
  if (best) {
    const button = el('button', 'btn primary small', t('guard.fit_button', {
      label: best.label, model: best.model, percent: Math.round(best.ratio * 100) }));
    button.onclick = () => fitSwitch(best);
    actions.appendChild(button);
  }
}

async function fitSwitch(pick) {
  try {
    const result = await api('fit_switch', { provider: pick.provider, model: pick.model });
    if (result.error) { toast(result.error, true); return; }
    if (result.state) STATE = result.state;
    SELECTED = pick.provider;
    renderHeader(); renderProviders(); renderGuardBanner();
    const item = STATE.providers.find((p) => p.id === pick.provider);
    if (item) renderDetail(item);
    toast(t('guard.fit_done', { label: pick.label, model: pick.model }));
  } catch (error) {
    toast(error.message, true);
  }
}

async function repairThreads(dryRun) {
  try {
    const result = await api('repair', dryRun ? { dry_run: true } : {});
    if (dryRun) {
      const lines = (result.items || []).map((item) =>
        `${item.id}  ${item.from} → ${item.to}  （${item.model}）`);
      alert(t('repair.preview_title', { count: result.items.length }) + '\n\n' + lines.join('\n'));
      return;
    }
    if (result.state) STATE = result.state;
    renderThreadBanner();
    toast(result.fixed ? t('repair.fixed', { count: result.fixed }) : t('repair.nothing'));
  } catch (error) {
    toast(error.message, true);
  }
}

function renderHeader() {
  const current = STATE.current || {};
  $('current-status').textContent = current.model_provider
    ? t('status.current', { provider: current.model_provider, model: current.model || t('status.unset') })
    : t('status.no_config');
  // 优先用服务端给的机器可读代号，按当前语言翻译；拿不到才退回服务端的中文串
  const backendKey = 'keys.' + String(STATE.secret_backend_id || '').replace(/-/g, '_');
  const backend = (STATE.secret_backend_id && I18N[LANG][backendKey])
    ? t(backendKey) : (STATE.secret_backend || '—');
  $('secret-backend').textContent = 'v' + (STATE.version || '?') + ' · '
    + t('topbar.secret', { backend: backend });
  $('about-version').textContent = t('status.version', { version: STATE.version || '?' });
  if (STATE.project_url) $('about-repo').href = STATE.project_url;
  renderUpdate(STATE.update);
}

function renderUpdate(info) {
  const node = $('about-update');
  if (!info) { node.textContent = ''; return; }
  if (info.status !== 'ok') { node.textContent = ''; return; }
  if (info.up_to_date) {
    node.className = 'hint';
    node.textContent = t('update.up_to_date');
  } else {
    node.className = 'hint update-new';
    node.textContent = t('update.available', { latest: info.latest, current: info.current });
  }
}

function renderProviders() {
  const list = $('provider-list');
  list.innerHTML = '';
  const needle = QUERY.trim().toLowerCase();
  const visible = STATE.providers.filter((provider) => {
    if (!needle) return true;
    const haystack = [provider.id, provider.label, ...(provider.models || [])].join(' ').toLowerCase();
    return haystack.includes(needle);
  });
  $('empty-hint').hidden = visible.length > 0;
  for (const provider of visible) {
    const card = el('div', 'provider-card' + (provider.id === SELECTED ? ' active' : ''));
    card.appendChild(el('div', 'provider-avatar', (provider.label || provider.id).slice(0, 1).toUpperCase()));
    const body = el('div', 'provider-body');
    const row = el('div', 'row');
    row.appendChild(el('span', 'name', provider.label || provider.id));
    row.appendChild(el('span', 'dot' + (provider.is_current ? ' on' : '')));
    body.appendChild(row);
    body.appendChild(el('div', 'meta', t('provider.meta', {
      count: provider.models.length,
      hint: provider.has_key ? provider.key_hint : t('provider.no_key'),
    })));
    card.appendChild(body);
    card.onclick = () => { SELECTED = provider.id; renderProviders(); renderDetail(provider); };
    list.appendChild(card);
  }
  renderProviderDocs();
}

// 左栏下半块：当前平台的接入说明。文案刻意写成「任何能提供 API 的平台都能接」，
// 不点名具体平台 —— 这个工具的定位是通用的，别让人以为只支持列表里这几家。
function renderProviderDocs() {
  const node = $('provider-docs');
  if (!node) return;
  const provider = (STATE.providers || []).find((p) => p.id === SELECTED);
  if (!provider) { node.hidden = true; node.innerHTML = ''; return; }
  const docs = provider.platform_docs || {};
  node.hidden = false;
  node.innerHTML = '';
  node.appendChild(el('h2', 'panel-title docs-title', t('docs.title')));
  // 三步接入：这些步骤对任何平台都一样，不挑厂商
  const steps = el('ol', 'docs-steps');
  for (const key of ['docs.step_add', 'docs.step_url', 'docs.step_key']) {
    steps.appendChild(el('li', null, t(key)));
  }
  node.appendChild(steps);
  const line = el('p', 'docs-hint', t('docs.generic', {
    url: provider.upstream_base_url || provider.base_url || '—',
    transport: transportLabel(provider.transport),
  }));
  node.appendChild(line);
  // 官方文档链接：有就给，没有也不影响接入
  if (docs.docs) {
    const link = el('a', 'docs-link', t('docs.open'));
    link.href = docs.docs;
    link.target = '_blank';
    link.rel = 'noreferrer';
    node.appendChild(link);
  }
  // 全量能力提醒：这个平台的 MCP / CLI 环境还没配齐的话，别让用户不知道
  const integrations = provider.integrations || {};
  const surface = provider.api_surface || [];
  if (integrations.available && !integrations.mcp) {
    const warn = el('div', 'docs-reminder');
    warn.appendChild(el('p', 'docs-reminder-text', t('docs.integrations_missing')));
    const btn = el('button', 'btn small docs-reminder-btn', t('caps.sync_button'));
    btn.addEventListener('click', () => syncIntegrations(provider));
    warn.appendChild(btn);
    node.appendChild(warn);
  } else if (integrations.mcp && surface.length) {
    node.appendChild(el('p', 'docs-hint', t('caps.mcp_ready')));
  }
}

function renderDetail(provider) {
  const models = visibleModels(provider);
  const detail = $('detail');
  detail.innerHTML = '';

  const head = el('div', 'detail-head');
  const info = el('div');
  info.appendChild(el('h2', null, provider.label || provider.id));
  info.appendChild(el('div', 'url',
    `${provider.upstream_base_url || provider.base_url} · ${transportLabel(provider.transport)}`));
  head.appendChild(info);

  const actions = el('div', 'detail-actions');
  const balanceButton = el('button', 'btn ghost', t('btn.balance'));
  balanceButton.onclick = () => queryBalance(provider, balanceButton);
  const refreshButton = el('button', 'btn ghost', t('btn.refresh'));
  refreshButton.onclick = () => refreshModels(provider.id, refreshButton);
  const useButton = el('button', 'btn primary', t('btn.use'));
  useButton.onclick = () => switchTo(provider.id, provider.default_model || provider.models[0]);
  const removeButton = el('button', 'btn danger', t('btn.remove'));
  removeButton.onclick = () => removeProvider(provider);
  actions.append(balanceButton, refreshButton, useButton, removeButton);
  head.appendChild(actions);
  detail.appendChild(head);

  const cards = el('div', 'cards');
  // 密钥、模型数、连接方式原来各占一张卡，三张卡在讲同一件小事，
  // 把有用的信息（额度 / 余额 / 上下文 / 思考强度）挤到下面去了。
  // 现在合成一张「概览」，一行读完。
  cards.appendChild(overviewCard(provider));

  if (provider.usage) {
    const usageCard = el('div', 'card');
    usageCard.appendChild(el('div', 'label', t('card.usage')));
    usageCard.appendChild(el('div', 'value', provider.usage.used_tokens_human || '0'));
    if (provider.usage.percent !== undefined) {
      usageCard.appendChild(el('div', 'note', t('card.quota_line', {
        total: provider.usage.quota_tokens_human,
        percent: provider.usage.percent,
        remain: provider.usage.remaining_tokens_human,
      })));
      const bar = el('div', 'progress' + (provider.usage.percent >= 90 ? ' err' : (provider.usage.percent >= 70 ? ' warn' : '')));
      const fill = el('span');
      fill.style.width = `${Math.max(2, provider.usage.percent)}%`;
      bar.appendChild(fill);
      usageCard.appendChild(bar);
    } else {
      usageCard.appendChild(el('div', 'note', t('card.quota_none')));
    }
    if (provider.local_usage) {
      usageCard.appendChild(el('div', 'note', t('card.turns', {
        sessions: provider.local_usage.sessions, turns: provider.local_usage.turns })));
    }
    const quotaButton = el('button', 'btn ghost small',
      provider.usage.quota_tokens ? t('card.quota_edit') : t('card.quota_set'));
    quotaButton.onclick = () => setQuota(provider);
    usageCard.appendChild(quotaButton);
    cards.appendChild(usageCard);
  } else if (provider.local_usage) {
    const usageCard = el('div', 'card');
    usageCard.appendChild(el('div', 'label', t('card.usage')));
    usageCard.appendChild(el('div', 'value', provider.local_usage.total_tokens_human));
    usageCard.appendChild(el('div', 'note', t('card.turns', {
      sessions: provider.local_usage.sessions, turns: provider.local_usage.turns })));
    cards.appendChild(usageCard);
  }

  if (provider.balance) {
    cards.appendChild(balanceCard(provider.balance));
  } else if (provider.console_url) {
    const card = el('div', 'card');
    card.appendChild(el('div', 'label', t('card.balance')));
    card.appendChild(el('div', 'value small', t('card.check_on_site')));
    const link = el('a', 'note', t('card.open_site'));
    link.href = provider.console_url;
    link.target = '_blank';
    link.rel = 'noreferrer';
    card.appendChild(link);
    cards.appendChild(card);
  }
  // 选中平台后自动查一次额度（每个平台每次打开只自动查一次，避免反复请求）
  if (!provider.balance && !BALANCE_TRIED.has(provider.id)) {
    BALANCE_TRIED.add(provider.id);
    setTimeout(() => queryBalance(provider, null), 80);
  }
  cards.appendChild(contextCard(provider));
  cards.appendChild(effortCard(provider));
  // 「能力查看」不再单占一张卡：顶部栏有按钮，下面每个模型也都有「能力」入口，
  // 这里再放一张卡只是重复一遍。
  detail.appendChild(cards);

  if (provider.notes) {
    detail.appendChild(el('div', 'notice', provider.notes));
  }

  const modelsHead = el('div', 'models-head');
  modelsHead.appendChild(el('h3', null, t('models.title')));
  const addModelButton = el('button', 'btn ghost', t('models.add'));
  addModelButton.onclick = () => addModel(provider.id);
  modelsHead.appendChild(addModelButton);
  detail.appendChild(modelsHead);

  const grid = el('div', 'models');
  if (!provider.models.length) {
    grid.appendChild(el('div', 'hint', t('models.empty')));
  } else if (!models.length) {
    grid.appendChild(el('div', 'hint', t('models.no_match', { query: QUERY })));
  }
  for (const model of models) {
    const item = el('div', 'model' + (provider.is_current && provider.default_model === model ? ' current' : ''));
    const left = el('div');
    left.appendChild(el('span', 'id', model));
    // 选模型时最该看到的就是「这个模型能装多少」：官方窗口 / Codex 实际可用 /
    // 建议的自动压缩线。三层都摊开，别让人猜为什么 1M 的模型只用得上 996K。
    const info = (provider.model_context || {})[model];
    if (info && info.window) {
      const line = el('span', 'tag ctx-tag', t('models.ctx_line', {
        window: info.window_human,
        effective: info.effective_human,
        percent: info.effective_percent,
        compact: info.compact_human,
      }));
      line.title = t('models.ctx_title', {
        window: info.window, effective: info.effective,
        percent: info.effective_percent, compact: info.compact,
        ratio: info.compact_ratio,
      });
      left.appendChild(line);
    }
    item.appendChild(left);
    const actions = el('div', 'model-actions');
    // 每个模型都能单独看能力：点进来就只看这一个，不会丢一堆别的模型进来
    const capsButton = el('button', 'btn ghost small', t('caps.view_model'));
    capsButton.onclick = () => setView('caps', { provider: provider.id, model: model });
    const button = el('button', 'btn ghost', t('models.switch'));
    button.onclick = () => switchTo(provider.id, model);
    actions.append(capsButton, button);
    item.appendChild(actions);
    grid.appendChild(item);
  }
  detail.appendChild(grid);

  detail.appendChild(el('div', 'notice', t('switch.notice')));
}

// 概览：密钥 / 模型数 / 连接方式，三项挤在一张卡里，一行读完
function overviewItem(label, value, note) {
  const item = el('div', 'overview-item');
  item.appendChild(el('div', 'overview-label', label));
  const main = el('div', 'overview-value', value);
  if (note) main.title = note;
  item.appendChild(main);
  return item;
}

function overviewCard(provider) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'label', t('card.overview')));
  const row = el('div', 'overview-row');
  row.appendChild(overviewItem(
    'API Key',
    provider.has_key ? provider.key_hint : t('provider.unconfigured'),
    t('provider.key_note')));
  // 老记录没有同步时间，但模型是实打实在的，不要显示成「尚未同步」那种异常状态
  row.appendChild(overviewItem(
    t('card.models'),
    String(provider.models.length),
    provider.models_synced_at
      ? t('card.synced_at', { time: provider.models_synced_at.replace('T', ' ') })
      : t('card.imported')));
  if (provider.transport === 'native') {
    row.appendChild(overviewItem(t('card.transport'), t('card.native'), t('card.native_note')));
  } else {
    const running = provider.bridge_running;
    const item = overviewItem(
      t('card.bridge'),
      running ? t('card.running') : t('card.not_running'),
      running ? t('card.bridge_note_on') : t('card.bridge_note_off'));
    if (!running) item.classList.add('warn');
    row.appendChild(item);
  }
  card.appendChild(row);
  return card;
}

function contextCard(provider) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'label', t('card.context')));
  card.appendChild(el('div', 'note', t('context.hint')));

  const row = el('div', 'context-row');
  const select = el('select');
  const windows = provider.model_windows || {};
  const names = provider.models && provider.models.length ? provider.models : Object.keys(windows);
  for (const name of names) {
    const option = el('option', null, name);
    option.value = name;
    if (name === provider.default_model) option.selected = true;
    select.appendChild(option);
  }
  const input = el('input');
  input.type = 'number';
  input.min = '4096';
  input.max = '10000000';
  input.step = '1024';
  const sync = () => { input.value = windows[select.value] || ''; };
  select.onchange = sync;
  sync();

  const save = el('button', 'btn ghost small', t('context.set'));
  save.onclick = async () => {
    const value = Number(input.value);
    if (!Number.isFinite(value) || value < 4096 || value > 10000000) {
      toast(t('context.invalid'), true);
      return;
    }
    try {
      const result = await api('set_context', {
        provider: provider.id, model: select.value, window: value });
      if (result.error) { toast(result.error, true); return; }
      if (result.state) STATE = result.state;
      renderDetail(STATE.providers.find((p) => p.id === provider.id) || provider);
      toast(t('context.saved', { model: select.value, tokens: String(value) }));
    } catch (error) { toast(error.message, true); }
  };

  const reset = el('button', 'btn ghost small', t('context.reset'));
  reset.onclick = async () => {
    try {
      const result = await api('set_context', {
        provider: provider.id, model: select.value, window: 0 });
      if (result.error) { toast(result.error, true); return; }
      if (result.state) STATE = result.state;
      renderDetail(STATE.providers.find((p) => p.id === provider.id) || provider);
      toast(t('context.reset_done'));
    } catch (error) { toast(error.message, true); }
  };

  row.append(select, input, save, reset);
  card.appendChild(row);
  return card;
}

const EFFORT_LEVELS = ['none', 'low', 'medium', 'high', 'xhigh'];

function effortCard(provider) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'label', t('card.effort')));
  card.appendChild(el('div', 'note', t('effort.hint')));

  const row = el('div', 'context-row');
  const select = el('select');
  const efforts = provider.model_efforts || {};
  const names = provider.models && provider.models.length ? provider.models : Object.keys(efforts);
  for (const name of names) {
    const option = el('option', null, name);
    option.value = name;
    if (name === provider.default_model) option.selected = true;
    select.appendChild(option);
  }

  const levelSelect = el('select');
  const sync = () => {
    const info = efforts[select.value] || {};
    const allowed = (info.levels && info.levels.length) ? info.levels : EFFORT_LEVELS;
    levelSelect.innerHTML = '';
    for (const level of allowed) {
      const option = el('option', null, t('effort.' + level));
      option.value = level;
      levelSelect.appendChild(option);
    }
    const current = info.current && allowed.includes(info.current)
      ? info.current : (allowed.includes('high') ? 'high' : allowed[0]);
    levelSelect.value = current;
  };
  select.onchange = sync;
  sync();

  const apply = el('button', 'btn ghost small', t('effort.set'));
  apply.onclick = async () => {
    try {
      const result = await api('set_effort', {
        provider: provider.id, model: select.value, effort: levelSelect.value });
      if (result.error) { toast(result.error, true); return; }
      if (result.state) STATE = result.state;
      renderDetail(STATE.providers.find((p) => p.id === provider.id) || provider);
      toast(t('effort.saved', { model: select.value, level: t('effort.' + levelSelect.value) }));
    } catch (error) { toast(error.message, true); }
  };

  const reset = el('button', 'btn ghost small', t('effort.reset'));
  reset.onclick = async () => {
    try {
      const result = await api('set_effort', {
        provider: provider.id, model: select.value, effort: '' });
      if (result.error) { toast(result.error, true); return; }
      if (result.state) STATE = result.state;
      renderDetail(STATE.providers.find((p) => p.id === provider.id) || provider);
      toast(t('effort.reset_done'));
    } catch (error) { toast(error.message, true); }
  };

  row.append(select, levelSelect, apply, reset);
  card.appendChild(row);
  return card;
}

function capChip(value, onClick) {
  const key = (value === 'yes' || value === 'no' || value === 'unknown') ? value : 'unknown';
  const node = el(onClick ? 'button' : 'span', 'chip ' + key + (onClick ? ' chip-btn' : ''), t('caps.' + key));
  if (onClick) {
    node.type = 'button';
    node.title = t('caps.annotate_hint');
    node.onclick = onClick;
  }
  return node;
}

// 点一下换一个值：未测出 → 支持 → 不支持 → 未测出
const CAP_CYCLE = { unknown: 'yes', yes: 'no', no: 'unknown' };

async function annotateCapability(provider, model, key, current) {
  const next = CAP_CYCLE[current] || 'yes';
  try {
    const result = await api('set_capability', {
      provider: provider.id, model: model, key: key, value: next });
    if (result.error) { toast(result.error, true); return; }
    if (result.state) STATE = result.state;
    renderCapsPage();
    const fresh = STATE.providers.find((p) => p.id === provider.id) || provider;
    if (SELECTED === provider.id) renderDetail(fresh);
    toast(t('caps.annotated', {
      model: model, cap: t('caps.' + key), value: t('caps.' + next) }));
  } catch (error) { toast(error.message, true); }
}

const CAP_COLUMNS = ['vision', 'reasoning', 'tools'];

function capsRow(provider, model, info) {
  const row = el('div', 'caps-table-row');
  row.appendChild(el('span', 'caps-model', model));
  for (const key of CAP_COLUMNS) {
    const cell = el('span', 'caps-cell');
    cell.appendChild(capChip(info[key], () => annotateCapability(provider, model, key, info[key])));
    row.appendChild(cell);
  }
  const ctx = (provider.model_context || {})[model] || {};
  const windowCell = el('span', 'caps-cell caps-num',
    ctx.window ? ctx.window_human : '—');
  if (ctx.window) windowCell.title = t('caps.window_title', { window: ctx.window });
  row.appendChild(windowCell);
  const effectiveCell = el('span', 'caps-cell caps-num',
    ctx.effective ? ctx.effective_human : '—');
  if (ctx.effective) {
    effectiveCell.title = t('caps.effective_title', {
      window: ctx.window, percent: ctx.effective_percent, effective: ctx.effective });
  }
  row.appendChild(effectiveCell);
  const compactCell = el('span', 'caps-cell caps-num',
    ctx.compact ? ctx.compact_human : '—');
  if (ctx.compact) {
    compactCell.title = t('caps.compact_title', {
      effective: ctx.effective, ratio: ctx.compact_ratio, compact: ctx.compact });
  }
  row.appendChild(compactCell);
  const source = info.source ? t('caps.source_' + info.source) : '';
  const sourceCell = el('span', 'caps-cell');
  if (source) sourceCell.appendChild(el('span', 'chip', source));
  // 官方文档怎么说，鼠标悬停就能看到原文，不用去翻文档
  if (info.documented) {
    const docChip = el('span', 'chip', t('caps.source_documented'));
    const parts = Object.keys(info.documented).map((key) => {
      const value = info.documented[key];
      if (key === 'context') return t('caps.col_window') + ' ' + shortTokens(value);
      return t('caps.' + key) + '：' + t('caps.' + (value === 'yes' ? 'yes' : 'no'));
    });
    docChip.title = parts.join(' · ') + (info.note ? ' — ' + info.note : '');
    sourceCell.appendChild(docChip);
  }
  if (info.conflict && info.conflict.length) {
    const clash = el('span', 'chip unknown', t('caps.conflict'));
    clash.title = t('caps.conflict_title', { keys: info.conflict.map((k) => t('caps.' + k)).join('、') });
    sourceCell.appendChild(clash);
  }
  row.appendChild(sourceCell);
  const action = el('span', 'caps-cell');
  const one = el('button', 'btn ghost small', t('caps.probe_this'));
  one.onclick = () => runProbe(provider, true, model);
  action.appendChild(one);
  row.appendChild(action);
  return row;
}

function renderCapsPage() {
  const body = $('caps-page-body');
  if (!body) return;
  // 这里兜底一次，保证任何刷新路径下焦点都落在一个真实存在的模型上
  syncCapsFocus();
  body.innerHTML = '';
  const providers = STATE.providers || [];
  if (!providers.length) {
    body.appendChild(el('div', 'hint', t('caps.empty')));
    return;
  }
  body.appendChild(capsFocusBar(providers));
  // 默认只渲染选中的那一个平台，展开全部时才是全量对照表
  const shown = CAPS_ALL || !CAPS_FOCUS
    ? providers
    : providers.filter((p) => p.id === CAPS_FOCUS.provider);
  if (!shown.length) {
    body.appendChild(el('div', 'hint', t('caps.focus_gone')));
    return;
  }
  for (const provider of shown) {
    const section = el('section', 'caps-section' + (provider.is_current ? ' current' : ''));
    const head = el('div', 'caps-section-head');
    const title = el('div', 'caps-section-title');
    title.appendChild(el('h3', null, provider.label || provider.id));
    if (provider.is_current) title.appendChild(el('span', 'chip yes', t('caps.current')));
    head.appendChild(title);
    const meta = el('div', 'caps-section-meta');
    const docs = provider.platform_docs || {};
    if (docs.hint) meta.appendChild(el('span', 'note', docs.hint));
    if (docs.docs) {
      const link = el('a', 'note', t('caps.docs_link'));
      link.href = docs.docs;
      link.target = '_blank';
      link.rel = 'noreferrer';
      meta.appendChild(link);
    }
    head.appendChild(meta);
    section.appendChild(head);

    const table = el('div', 'caps-table caps-table-full');
    const header = el('div', 'caps-table-row caps-table-head');
    header.appendChild(el('span', 'caps-model', t('caps.col_model')));
    for (const key of CAP_COLUMNS) header.appendChild(el('span', 'caps-cell', t('caps.' + key)));
    header.appendChild(el('span', 'caps-cell caps-num', t('caps.col_window')));
    header.appendChild(el('span', 'caps-cell caps-num', t('caps.col_effective')));
    header.appendChild(el('span', 'caps-cell caps-num', t('caps.col_compact')));
    header.appendChild(el('span', 'caps-cell', t('caps.col_source')));
    header.appendChild(el('span', 'caps-cell', ''));
    table.appendChild(header);

    const caps = provider.model_capabilities || {};
    const all = provider.models || [];
    // 只看一个模型时，表格里就只有那一行 —— 这才是用户要看的东西
    const models = (CAPS_ALL || !CAPS_FOCUS)
      ? all
      : all.filter((m) => m === CAPS_FOCUS.model);
    for (const model of models) {
      const info = caps[model] || { vision: 'unknown', reasoning: 'unknown', tools: 'unknown' };
      table.appendChild(capsRow(provider, model, info));
    }
    if (!all.length) table.appendChild(el('div', 'hint', t('models.empty')));
    else if (!models.length) table.appendChild(el('div', 'hint', t('caps.focus_gone')));
    section.appendChild(table);

    // 平台全量 API 能力：同一把 Key 在 Codex 之外还能干什么
    // （生图 / 生视频 / 语音 / 联网搜索 / 官方 MCP / CLI）
    section.appendChild(capsSurfaceBlock(provider));

    const hint = el('div', 'caps-section-note');
    if (provider.notes) hint.appendChild(el('div', 'note', provider.notes));
    hint.appendChild(el('div', 'note', t('caps.probe_cost')));
    hint.appendChild(el('div', 'note', t('caps.note_openai_only')));
    hint.appendChild(el('div', 'note', t('caps.note_generation')));
    section.appendChild(hint);
    body.appendChild(section);
  }
}

function capsSurfaceBlock(provider) {
  const wrap = el('div', 'caps-surface');
  const head = el('div', 'caps-surface-head');
  head.appendChild(el('h4', null, t('caps.surface_title')));
  head.appendChild(el('span', 'note', t('caps.surface_hint')));
  wrap.appendChild(head);
  const surface = provider.api_surface || [];
  if (!surface.length) {
    wrap.appendChild(el('div', 'hint', t('caps.surface_empty')));
    return wrap;
  }
  const list = el('div', 'caps-surface-list');
  for (const item of surface) {
    const row = el('div', 'caps-surface-row');
    const chip = el('span', 'chip ' + (item.status === 'yes' ? 'yes' : item.status === 'no' ? 'no' : 'unknown'),
      t('caps.' + item.status));
    row.appendChild(chip);
    row.appendChild(el('span', 'caps-surface-name', t('surface.' + item.key)));
    if (item.note) row.appendChild(el('span', 'caps-surface-note', item.note));
    if (item.url) {
      const link = el('a', 'caps-surface-link', t('caps.docs_link'));
      link.href = item.url;
      link.target = '_blank';
      link.rel = 'noreferrer';
      row.appendChild(link);
    }
    list.appendChild(row);
  }
  wrap.appendChild(list);
  const status = provider.integrations || {};
  if (status.mcp) wrap.appendChild(el('div', 'note', t('caps.mcp_ready')));
  else if (status.available) {
    const btn = el('button', 'btn small', t('caps.sync_button'));
    btn.addEventListener('click', () => syncIntegrations(provider));
    wrap.appendChild(btn);
  }
  return wrap;
}

async function syncIntegrations(provider) {
  toast(t('caps.syncing'));
  try {
    const result = await api('sync_integrations', { provider: provider.id });
    if (result.error) { toast(t('caps.sync_failed', { error: result.error }), true); return; }
    if (result.state) STATE = result.state;
    if (VIEW === 'caps') renderCapsPage();
    toast(t('caps.sync_done'));
  } catch (error) { toast(t('caps.sync_failed', { error: error.message }), true); }
}

async function runProbe(provider, doApply, model) {
  toast(model ? t('caps.probing_one', { model: model }) : t('caps.probing'));
  try {
    const payload = { provider: provider.id, apply: doApply };
    if (model) payload.model = model;
    const result = await api('probe_capabilities', payload);
    if (result.error) { toast(t('caps.probe_failed', { error: result.error }), true); return; }
    if (result.state) STATE = result.state;
    if (VIEW === 'caps') renderCapsPage();
    const fresh = STATE.providers.find((p) => p.id === provider.id) || provider;
    if (SELECTED === provider.id) renderDetail(fresh);
    toast(t('caps.probe_done'));
  } catch (error) { toast(t('caps.probe_failed', { error: error.message }), true); }
}

function balanceCard(balance) {
  const card = el('div', 'card');
  if (balance.status === 'ok') card.classList.add('ok');
  else if (balance.status === 'error') card.classList.add('err');
  else card.classList.add('warn');
  card.appendChild(el('div', 'label', t('card.balance')));
  card.appendChild(el('div', 'value', balance.display || '—'));
  if (balance.message) card.appendChild(el('div', 'note', balance.message));
  for (const field of balance.fields || []) {
    card.appendChild(el('div', 'note', `${field.label}：${field.value}`));
  }
  if (balance.console_url) {
    const link = el('a', 'note', t('card.open_site'));
    link.href = balance.console_url;
    link.target = '_blank';
    link.rel = 'noreferrer';
    card.appendChild(link);
  }
  return card;
}

function transportLabel(transport) {
  if (transport === 'native') return t('transport.native');
  if (transport === 'bridge') return t('transport.bridge');
  return t('transport.auto');
}

function visibleModels(provider) {
  const needle = QUERY.trim().toLowerCase();
  if (!needle) return provider.models;
  const providerMatches = [provider.id, provider.label || ''].join(' ').toLowerCase().includes(needle);
  if (providerMatches) return provider.models;
  return provider.models.filter((model) => model.toLowerCase().includes(needle));
}

async function setQuota(provider) {
  const current = provider.usage && provider.usage.quota_tokens;
  const raw = prompt(t('prompt.quota'), current ? String(current) : '');
  if (raw === null) return;
  const trimmed = raw.trim();
  try {
    const payload = trimmed
      ? { provider: provider.id, tokens: Number(trimmed) }
      : { provider: provider.id, clear: true };
    if (trimmed && (!Number.isFinite(payload.tokens) || payload.tokens <= 0)) {
      toast(t('toast.positive_int'), true);
      return;
    }
    const result = await api('quota', payload);
    STATE = result.state;
    const item = STATE.providers.find((p) => p.id === provider.id);
    if (item) { provider.usage = item.usage; renderDetail(provider); }
    toast(t('toast.quota_updated'));
  } catch (error) {
    toast(error.message, true);
  }
}

async function queryBalance(provider, button) {
  if (button) {
    button.disabled = true;
    button.textContent = t('btn.balancing');
  }
  try {
    const result = await api('balance', { provider: provider.id });
    provider.balance = result;
    renderDetail(provider);
  } catch (error) {
    toast(error.message, true);
    if (button) {
      button.disabled = false;
      button.textContent = t('btn.balance');
    }
  }
}

async function refreshModels(providerId, button) {
  button.disabled = true;
  button.textContent = t('btn.refreshing');
  try {
    const result = await api('refresh', { provider: providerId });
    toast(t('toast.models_updated', { count: result.models.length }));
    STATE = result.state;
    renderHeader(); renderProviders();
    const provider = STATE.providers.find((p) => p.id === providerId);
    if (provider) renderDetail(provider);
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = t('btn.refresh');
  }
}

async function switchTo(provider, model) {
  try {
    const result = await api('switch', { provider, model });
    toast(t('toast.switched', {
      label: result.label || result.provider, model: result.model }));
    // 旧任务跟着搬了没有，得让用户看见：不然切完继续任务还是报 unknown model
    const followed = result.threads_followed;
    if (followed && followed.moved) {
      const pending = (followed.skipped_active || []).length;
      setTimeout(() => toast(pending
        ? t('toast.follow_pending', { n: pending })
        : t('toast.followed', { n: followed.moved, label: result.label || result.provider }),
      ), 900);
    }
    // 后台还在分批搬老任务（含每日定时任务），也要让用户知道
    if (result.full_follow && result.full_follow.scheduled) {
      setTimeout(() => toast(t('toast.follow_background',
        { label: result.label || result.provider })), 1800);
    }
    STATE = result.state;
    SELECTED = provider;
    // 刚切过去的模型就是用户关心的那个，能力查看跟着切过去
    if (model) { CAPS_FOCUS = { provider: provider, model: model }; CAPS_ALL = false; }
    renderHeader(); renderProviders();
    const item = STATE.providers.find((p) => p.id === provider);
    if (item) renderDetail(item);
    if (VIEW === 'caps') renderCapsPage();
    // Codex 只在启动时读一次配置：切完弹窗提醒重启，点确认自动重启
    openRestartModal();
  } catch (error) {
    toast(error.message, true);
  }
}

function openRestartModal() {
  const node = $('restart-modal');
  if (!node) return;
  node.hidden = false;
  const body = $('restart-modal-body');
  if (body) body.textContent = t('restart.body');
}

function closeRestartModal() {
  const node = $('restart-modal');
  if (node) node.hidden = true;
}

async function restartCodexNow() {
  closeRestartModal();
  toast(t('restart.doing'));
  try {
    const result = await api('restart_codex', {});
    if (result.error) { toast(t('restart.failed', { error: result.error }), true); return; }
    if (result.restarted) toast(t('restart.done'));
    else toast(result.detail || t('restart.manual'), true);
  } catch (error) {
    toast(t('restart.failed', { error: error.message }), true);
  }
}

async function removeProvider(provider) {
  if (!confirm(t('confirm.delete', { label: provider.label || provider.id }))) return;
  try {
    const result = await api('remove', { provider: provider.id });
    toast(t('toast.deleted'));
    STATE = result.state;
    SELECTED = null;
    renderHeader(); renderProviders();
    $('detail').innerHTML = '<div class="placeholder"></div>';
    $('detail').firstChild.textContent = t('detail.pick_provider');
  } catch (error) {
    toast(error.message, true);
  }
}

async function addModel(providerId) {
  const raw = prompt(t('prompt.model'));
  if (!raw) return;
  const models = raw.split(',').map((item) => item.trim()).filter(Boolean);
  if (!models.length) return;
  try {
    const result = await api('add-model', { provider: providerId, models });
    toast(t('toast.models_added', { count: models.length }));
    STATE = result.state;
    renderProviders();
    const provider = STATE.providers.find((p) => p.id === providerId);
    if (provider) renderDetail(provider);
  } catch (error) {
    toast(error.message, true);
  }
}

/* ------------------------------------------------------------------ 手动添加 */

function openManual() {
  $('manual-modal').hidden = false;
  $('form-message').textContent = '';
  fillPresets();
}

function closeManual() { $('manual-modal').hidden = true; }

function fillPresets() {
  const select = $('f-preset');
  const keep = select.value;
  select.innerHTML = '';
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = t('modal.preset_none');
  select.appendChild(placeholder);
  for (const preset of STATE.presets || []) {
    const option = document.createElement('option');
    option.value = preset.id;
    option.textContent = preset.label + (preset.has_balance_api ? t('preset.balance_badge') : '');
    select.appendChild(option);
  }
  select.value = keep;
  select.onchange = () => {
    const preset = (STATE.presets || []).find((item) => item.id === select.value);
    if (!preset) return;
    $('f-label').value = preset.label;
    $('f-base-url').value = preset.base_url || '';
    $('f-transport').value = preset.transport || 'auto';
    $('f-console-url').value = preset.console_url || '';
    $('f-requires-key').checked = preset.requires_key !== false;
    if ((preset.known_models || []).length) $('f-models').value = preset.known_models.join('\n');
  };
}

async function saveManual() {
  const message = $('form-message');
  const payload = {
    preset_id: $('f-preset').value || null,
    label: $('f-label').value.trim(),
    base_url: $('f-base-url').value.trim(),
    models_url: $('f-models-url').value.trim(),
    key: $('f-key').value.trim(),
    transport: $('f-transport').value,
    console_url: $('f-console-url').value.trim(),
    requires_key: $('f-requires-key').checked,
    balance_url: $('f-balance-url').value.trim(),
    balance_path: $('f-balance-path').value.trim(),
    balance_currency: $('f-balance-currency').value.trim(),
    models: $('f-models').value.split('\n').map((item) => item.trim()).filter(Boolean),
  };
  if (!payload.base_url) { message.textContent = t('form.need_base_url'); return; }
  message.textContent = t('form.fetching');
  try {
    const result = await api('add', payload);
    STATE = result.state;
    renderHeader(); renderProviders();
    const provider = STATE.providers.find((p) => p.id === result.provider);
    if (provider) { SELECTED = provider.id; renderProviders(); renderDetail(provider); }
    closeManual();
    $('f-key').value = '';
    if (result.discovery_error) {
      toast(t('toast.saved_discovery_failed', { error: result.discovery_error }), true);
    } else {
      toast(t('toast.saved', { count: result.models.length }));
    }
  } catch (error) {
    message.textContent = error.message;
  }
}

/* ---------------------------------------------------------------------- 绑定 */

$('btn-manual').onclick = openManual;
$('btn-repair-threads').onclick = () => repairThreads(false);
$('btn-thread-preview').onclick = () => repairThreads(true);
$('platform-search').addEventListener('input', (event) => {
  QUERY = event.target.value;
  renderProviders();
  if (SELECTED) {
    const item = STATE.providers.find((p) => p.id === SELECTED);
    if (item) renderDetail(item);
  }
});
$('btn-close-manual').onclick = closeManual;
$('btn-save-manual').onclick = saveManual;
const restartModal = $('restart-modal');
if (restartModal) {
  $('btn-restart-now').onclick = restartCodexNow;
  $('btn-restart-later').onclick = closeRestartModal;
  $('btn-close-restart').onclick = closeRestartModal;
  restartModal.addEventListener('click', (event) => {
    if (event.target === restartModal) closeRestartModal();
  });
}
$('btn-refresh-all').onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = t('btn.refreshing_all');
  try {
    await api('refresh', { provider: 'all' });
  } catch (error) {
    /* 单个平台的失败不影响其它平台 */
  }
  await loadState();
  button.disabled = false;
  button.textContent = t('topbar.refresh_all');
  toast(t('toast.all_refreshed'));
};
$('btn-restore').onclick = async () => {
  if (!confirm(t('confirm.restore'))) return;
  try {
    await api('restore', {});
    await loadState();
    toast(t('toast.restored'));
  } catch (error) {
    toast(error.message, true);
  }
};

$('btn-check-update').onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = t('update.checking');
  try {
    const result = await api('update-check', {});
    renderUpdate(result);
    toast(t('update.checked'));
  } catch (error) {
    toast(t('update.failed', { message: error.message }), true);
  }
  button.disabled = false;
  button.textContent = t('update.check');
};

/* ---------------------------------------------------------------- 语言切换 */

function renderFooterHelp() {
  const node = $('footer-help');
  if (!node) return;
  // 这句里有 <code>，所以用 innerHTML；文本全部来自词典，不含用户输入
  node.innerHTML = t('footer.help')
    .split('{doctor}').join('<code>codex-switcher doctor</code>')
    .split('{stop}').join('<code>codex-switcher stop</code>');
}

$('btn-lang').onclick = toggleLang;
$('btn-caps-page').onclick = () => setView(VIEW === 'caps' ? 'providers' : 'caps');
$('btn-caps-back').onclick = () => setView('providers');

window.addEventListener('langchange', () => {
  renderFooterHelp();
  renderHeader();
  renderThreadBanner();
  renderGuardBanner();
  renderProviders();
  $('btn-caps-page').textContent = t('topbar.caps_page');
  if (VIEW === 'caps') renderCapsPage();
  if (SELECTED) {
    const item = STATE.providers.find((p) => p.id === SELECTED);
    if (item) renderDetail(item);
  } else {
    $('detail').innerHTML = '<div class="placeholder"></div>';
    $('detail').firstChild.textContent = t('detail.pick_provider');
  }
  // 表单里的预设下拉是动态填充的，语言变了要重建
  fillPresets();
});

applyI18n();
renderFooterHelp();

// 支持 ?view=caps 直接打开能力查看页（演示 GIF 和书签都用得上）
const initialView = new URLSearchParams(location.search).get('view');
if (initialView === 'caps') setView('caps');

loadState().catch((error) => toast(error.message, true));
