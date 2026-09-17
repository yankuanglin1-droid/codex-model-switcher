/* 界面文案词典：中文 / English
 *
 * 用法：
 *   HTML 里写 data-i18n="key"（文本）、data-i18n-placeholder="key"、
 *   data-i18n-title="key"（鼠标悬停提示）
 *   JS  里用 t('key', { 变量: 值 })
 *
 * 切换语言后派发 window 上的 'langchange' 事件，app.js 收到后重渲染动态内容。
 */

const I18N = {
  zh: {
    'lang.other': 'English',
    'lang.title': '切换界面语言',

    'app.title': 'codex（ChatGPT App）多平台模型切换',
    'topbar.status_loading': '正在读取状态…',
    'topbar.search': '搜索平台或模型…',
    'topbar.secret': '密钥存储：{backend}',
    'keys.keychain': 'macOS 钥匙串',
    'keys.secret_service': 'Linux Secret Service',
    'keys.dpapi': 'Windows 凭据加密（DPAPI）',
    'keys.file': '本地文件（0600，保护最弱）',
    'topbar.refresh_all': '全部刷新模型',
    'topbar.restore': '恢复官方 OpenAI',
    'topbar.add_manual': '手动添加平台',

    'banner.title': '检测到任务绑定异常',
    'banner.detail': '{n} 个旧任务的服务商和模型对不上',
    'banner.note': '在这些任务里换模型会报 “model is not supported”。一键修复后即可正常继续，改动前会自动备份。',
    'banner.preview': '查看明细',
    'banner.repair': '一键修复',

    'guard.title': '当前对话装不下目标模型的上下文窗口',
    'guard.detail': '{tokens} tokens / 可用 {window} tokens（{percent}%）：{advice}',
    'guard.note_fork': '直接续接会陷入反复压缩 —— 压完还超、超限又压。请新开任务，或先分叉再换模型。',
    'guard.note_compact': '体量已经贴着窗口上限，续接前先手动压缩一次。',
    'guard.action_fork': '必须分叉或新开任务',
    'guard.action_compact': '续接前先压缩一次',

    'card.context': '上下文窗口',
    'context.hint': '窗口写小了会反复压缩，写大了会被服务商拒绝。改完立刻重生成目录。',
    'context.set': '保存',
    'context.reset': '恢复默认',
    'context.saved': '已保存 {model}：{tokens} tokens',
    'context.reset_done': '已恢复按模型名推断',
    'context.invalid': '请填 4096 到 10000000 之间的数字',

    'card.effort': '思考强度',
    'effort.hint': '选好档位会同时写进模型目录和 Codex 配置；第三方平台不认的档位会被忽略。',
    'effort.set': '应用',
    'effort.reset': '恢复默认',
    'effort.saved': '已设置 {model}：{level}',
    'effort.reset_done': '已恢复平台默认档位',
    'effort.none': '不思考（最快）',
    'effort.low': '轻度思考',
    'effort.medium': '标准思考',
    'effort.high': '深度思考',
    'effort.xhigh': '超深度思考',
    'effort.unknown': '未知档位',

    'card.caps': '模型能力',
    'caps.hint': '「实测」= 拿真实请求测过；「推断」= 只按模型名猜的，可能不准。',
    'caps.vision': '读图',
    'caps.reasoning': '思考',
    'caps.tools': '工具',
    'caps.yes': '支持',
    'caps.no': '不支持',
    'caps.unknown': '未测出',
    'caps.source_verified': '实测',
    'caps.source_measured': '本机实测',
    'caps.source_inferred': '推断',
    'caps.source_documented': '官方',
    'caps.source_manual': '手动指定',
    'caps.probe': '实测',
    'caps.probe_apply': '实测并写回',
    'caps.probing': '正在实测…',
    'caps.probe_done': '实测完成，结论已写回目录',
    'caps.probe_failed': '实测失败：{error}',
    'caps.vision_yes': '能读图',
    'caps.vision_no': '不能读图',
    'caps.note_openai_only': '联网搜索、画图、操作电脑是 OpenAI 独有工具，第三方平台一律没有。',
    'caps.note_generation': '画图 / 生成视频 / 语音合成是各家平台上的独立模型与独立接口（如 image-01、video-01、speech-02），Codex 只连一个对话模型，用不到它们。',

    'sidebar.title': '已接入平台',
    'sidebar.empty': '还没有接入任何平台。点右上角「手动添加平台」，或从预设里选一个开始。',
    'detail.placeholder': '从左边选一个平台，查看它的模型与额度。',
    'detail.pick_provider': '从左边选一个平台。',

    'status.current': '当前默认：{provider} · {model}',
    'status.unset': '未设置',
    'status.no_config': '还没有检测到 Codex 配置',
    'status.version': 'codex（ChatGPT App）多平台模型切换 v{version}',

    'update.up_to_date': '已是最新版本',
    'update.available': '有新版本 {latest}（当前 v{current}）',
    'update.checking': '检查中…',
    'update.check': '检查更新',
    'update.checked': '已检查',
    'update.failed': '检查更新失败：{message}',

    'provider.meta': '{count} 个模型 · {hint}',
    'provider.no_key': '未配置密钥',
    'provider.unconfigured': '未配置',
    'provider.key_note': '只存在系统钥匙串，不写进配置文件',

    'card.models': '可用模型',
    'card.synced_at': '更新于 {time}',
    'card.imported': '已从平台导入，可随时点「刷新模型」更新',
    'card.transport': '连接方式',
    'card.native': '原生 Responses',
    'card.native_note': '平台自带 Codex 需要的接口，直连最快',
    'card.bridge': '本地协议桥',
    'card.running': '运行中',
    'card.not_running': '未运行',
    'card.bridge_note_on': 'Codex 通过本机 127.0.0.1:8787 访问该平台',
    'card.bridge_note_off': '该平台只支持 Chat Completions，未运行协议桥时请求会失败',
    'card.usage': '本机用量（近似）',
    'card.quota_line': '套餐额度 {total} · 已用 {percent}% · 约剩 {remain}',
    'card.quota_none': '没设套餐额度，只能看已用量；点下面按钮可以补上',
    'card.turns': '{sessions} 个任务 · {turns} 轮对话',
    'card.quota_edit': '修改套餐额度',
    'card.quota_set': '设置套餐额度',
    'card.balance': '余额 / 额度',
    'card.check_on_site': '需在官网查看',
    'card.open_site': '打开官网 ↗',

    'models.title': '模型列表',
    'models.add': '手动加模型',
    'models.empty': '这个平台还没有模型。点「刷新模型」，或用「手动加模型」。',
    'models.no_match': '没有匹配「{query}」的模型。',
    'models.switch': '切换',

    'btn.balance': '查询额度',
    'btn.balancing': '查询中…',
    'btn.refresh': '刷新模型',
    'btn.refreshing': '更新中…',
    'btn.refreshing_all': '刷新中…',
    'btn.use': '设为默认平台',
    'btn.remove': '删除平台',
    'btn.save': '保存并拉取模型',

    'transport.native_label': '直连 Responses（平台自带）',
    'transport.bridge_label': '本地协议桥（只支持 Chat Completions 的平台）',
    'transport.auto_label': '自动探测（推荐）',
    'transport.native': '原生 Responses 直连',
    'transport.bridge': '本地协议桥',
    'transport.auto': '自动探测',

    'switch.notice': '切换成功后要完全退出（⌘Q）并重新打开 Codex 才生效。已经存在的旧任务仍绑定原来的平台，不会跟着切换；要在旧对话里继续，请对它使用「分叉」，或新建任务。',

    'toast.models_updated': '已更新 {count} 个模型',
    'toast.switched': '已切换为 {label} · {model}',
    'toast.deleted': '已删除',
    'toast.models_added': '已加入 {count} 个模型',
    'toast.saved': '已接入 {count} 个模型',
    'toast.saved_discovery_failed': '平台已保存，但自动获取模型失败：{error}',
    'toast.all_refreshed': '模型列表已刷新',
    'toast.restored': '已恢复官方 OpenAI，重启 Codex 后生效',
    'toast.quota_updated': '已更新额度设置',
    'toast.positive_int': '请输入正整数',
    'toast.language': '界面语言已切换为中文',

    'confirm.delete': '确定删除「{label}」？\n会同时移除配置并删除钥匙串里的密钥。',
    'confirm.restore': '恢复成官方 OpenAI 登录？\n第三方配置会保留，随时可以再切回去。',
    'prompt.quota': '输入这个平台套餐的总 token 数（例如 500000000）。\n留空表示清除设置。\n\n这个数字由你自己填，工具只用它来算本机用量占比，不会去猜。',
    'prompt.model': '输入模型名，多个用逗号分隔：',

    'repair.preview_title': '预演：将修复 {count} 个任务',
    'repair.fixed': '已修复 {count} 个任务，重启 Codex 后生效',
    'repair.nothing': '没有需要修复的任务',

    'preset.balance_badge': '（支持额度查询）',
    'form.need_base_url': '请填写 Base URL。',
    'form.fetching': '正在拉取模型…',

    'footer.repo': 'GitHub 项目主页 ↗',
    'footer.help': '出问题先跑 {doctor}；停掉后台服务用 {stop}',
    'modal.title': '手动添加平台',
    'modal.hint': '只需要平台名称和 Base URL 就能接入。API Key 会存进系统钥匙串，不会写进 Codex 配置文件。本机模型（Ollama / LM Studio）可以不填 Key。',
    'modal.preset': '从预设开始（可选）',
    'modal.preset_none': '— 自己填写 —',
    'modal.label': '平台名称 *',
    'modal.label_ph': '例如 我的中转站',
    'modal.base_url': 'Base URL *',
    'modal.models_url': '模型列表地址（留空自动推断）',
    'modal.key': 'API Key（存进钥匙串）',
    'modal.transport': '连接方式',
    'modal.models': '模型清单（每行一个；留空则自动拉取）',
    'modal.console_url': '官网地址（选填，用于查额度）',
    'modal.balance_url': '余额接口（选填）',
    'modal.balance_path': '余额字段路径',
    'modal.currency': '币种',
    'modal.needs_key': '这个平台需要 API Key',
  },

  en: {
    'lang.other': '中文',
    'lang.title': 'Switch interface language',

    'app.title': 'Codex (ChatGPT App) Multi-Provider Model Switcher',
    'topbar.status_loading': 'Reading status…',
    'topbar.search': 'Search providers or models…',
    'topbar.secret': 'Keys: {backend}',
    'keys.keychain': 'macOS keychain',
    'keys.secret_service': 'Linux Secret Service',
    'keys.dpapi': 'Windows DPAPI',
    'keys.file': 'local file (0600, weakest)',
    'topbar.refresh_all': 'Refresh all models',
    'topbar.restore': 'Back to official OpenAI',
    'topbar.add_manual': 'Add a provider',

    'banner.title': 'Some tasks are pinned to the wrong provider',
    'banner.detail': '{n} existing tasks have a provider/model mismatch',
    'banner.note': 'Switching models inside those tasks fails with “model is not supported”. Repair fixes it in place, and backs up before touching anything.',
    'banner.preview': 'Show details',
    'banner.repair': 'Repair now',

    'guard.title': 'This thread does not fit the target model context window',
    'guard.detail': '{tokens} tokens / {window} usable ({percent}%): {advice}',
    'guard.note_fork': 'Continuing it will loop on compaction forever. Start a new task, or fork this one before switching.',
    'guard.note_compact': 'Close to the limit — compact once before you continue.',
    'guard.action_fork': 'fork or start a new task',
    'guard.action_compact': 'compact first',

    'card.context': 'Context window',
    'context.hint': 'Too small loops on compaction; too large gets rejected. Saving regenerates the catalog right away.',
    'context.set': 'Save',
    'context.reset': 'Reset',
    'context.saved': 'Saved {model}: {tokens} tokens',
    'context.reset_done': 'Reset to the value inferred from the model name',
    'context.invalid': 'Enter a number between 4096 and 10000000',

    'card.effort': 'Reasoning effort',
    'effort.hint': 'The level is written to both the catalog and the Codex config. Providers that do not understand it simply ignore it.',
    'effort.set': 'Apply',
    'effort.reset': 'Reset',
    'effort.saved': 'Set {model}: {level}',
    'effort.reset_done': 'Reset to the provider default',
    'effort.none': 'Off (fastest)',
    'effort.low': 'Low',
    'effort.medium': 'Medium',
    'effort.high': 'High',
    'effort.xhigh': 'Extra high',
    'effort.unknown': 'Unknown level',

    'card.caps': 'Model capabilities',
    'caps.hint': '“Measured” means probed with real requests; “Inferred” is guessed from the model name and may be wrong.',
    'caps.vision': 'Vision',
    'caps.reasoning': 'Reasoning',
    'caps.tools': 'Tools',
    'caps.yes': 'yes',
    'caps.no': 'no',
    'caps.unknown': 'unknown',
    'caps.source_verified': 'measured',
    'caps.source_measured': 'measured here',
    'caps.source_inferred': 'inferred',
    'caps.source_documented': 'official',
    'caps.source_manual': 'manual',
    'caps.probe': 'Probe',
    'caps.probe_apply': 'Probe and apply',
    'caps.probing': 'Probing…',
    'caps.probe_done': 'Probe finished; results written back to the catalog',
    'caps.probe_failed': 'Probe failed: {error}',
    'caps.vision_yes': 'reads images',
    'caps.vision_no': 'cannot read images',
    'caps.note_openai_only': 'Web search, image generation and computer use are OpenAI-only; no third-party provider has them.',
    'caps.note_generation': 'Image, video and speech generation are separate models behind separate APIs (image-01, video-01, speech-02). Codex talks to one chat model and cannot reach them.',

    'sidebar.title': 'Connected providers',
    'sidebar.empty': 'No providers yet. Use “Add a provider” in the top right, or start from a preset.',
    'detail.placeholder': 'Pick a provider on the left to see its models and balance.',
    'detail.pick_provider': 'Pick a provider on the left.',

    'status.current': 'Currently: {provider} · {model}',
    'status.unset': 'not set',
    'status.no_config': 'No Codex config found yet',
    'status.version': 'Codex (ChatGPT App) Multi-Provider Model Switcher v{version}',

    'update.up_to_date': 'Up to date',
    'update.available': 'Version {latest} available (you have v{current})',
    'update.checking': 'Checking…',
    'update.check': 'Check for updates',
    'update.checked': 'Checked',
    'update.failed': 'Update check failed: {message}',

    'provider.meta': '{count} models · {hint}',
    'provider.no_key': 'no key',
    'provider.unconfigured': 'not set',
    'provider.key_note': 'Stored in the system keychain only, never in the config file',

    'card.models': 'Models available',
    'card.synced_at': 'Synced {time}',
    'card.imported': 'Imported from the provider — hit “Refresh models” any time',
    'card.transport': 'Connection',
    'card.native': 'Native Responses',
    'card.native_note': 'The provider speaks the protocol Codex needs — a direct connection is fastest',
    'card.bridge': 'Local bridge',
    'card.running': 'running',
    'card.not_running': 'not running',
    'card.bridge_note_on': 'Codex reaches this provider through 127.0.0.1:8787 on this machine',
    'card.bridge_note_off': 'This provider only speaks Chat Completions; requests fail while the bridge is down',
    'card.usage': 'Local usage (approx.)',
    'card.quota_line': 'Plan {total} · {percent}% used · ~{remain} left',
    'card.quota_none': 'No plan size set, so only raw usage is shown. Use the button below to add one.',
    'card.turns': '{sessions} tasks · {turns} turns',
    'card.quota_edit': 'Edit plan size',
    'card.quota_set': 'Set plan size',
    'card.balance': 'Balance / quota',
    'card.check_on_site': 'Check on the provider site',
    'card.open_site': 'Open provider site ↗',

    'models.title': 'Model list',
    'models.add': 'Add a model',
    'models.empty': 'No models yet. Use “Refresh models”, or “Add a model”.',
    'models.no_match': 'No model matches “{query}”.',
    'models.switch': 'Switch',

    'btn.balance': 'Check balance',
    'btn.balancing': 'Checking…',
    'btn.refresh': 'Refresh models',
    'btn.refreshing': 'Refreshing…',
    'btn.refreshing_all': 'Refreshing…',
    'btn.use': 'Make default',
    'btn.remove': 'Remove',
    'btn.save': 'Save and fetch models',

    'transport.native_label': 'Direct Responses (provider supports it)',
    'transport.bridge_label': 'Local bridge (provider only speaks Chat Completions)',
    'transport.auto_label': 'Detect automatically (recommended)',
    'transport.native': 'Direct Responses',
    'transport.bridge': 'Local bridge',
    'transport.auto': 'Auto-detect',

    'switch.notice': 'Quit Codex completely (⌘Q) and reopen it for the switch to take effect. Existing tasks stay pinned to their original provider — to continue one of those, fork it or start a new task.',

    'toast.models_updated': 'Updated {count} models',
    'toast.switched': 'Switched to {label} · {model}',
    'toast.deleted': 'Removed',
    'toast.models_added': 'Added {count} models',
    'toast.saved': 'Connected — {count} models',
    'toast.saved_discovery_failed': 'Provider saved, but fetching models failed: {error}',
    'toast.all_refreshed': 'Model lists refreshed',
    'toast.restored': 'Back on official OpenAI — restart Codex to apply',
    'toast.quota_updated': 'Plan size updated',
    'toast.positive_int': 'Enter a positive whole number',
    'toast.language': 'Interface language set to English',

    'confirm.delete': 'Remove “{label}”?\nThis deletes the config block and the key in your keychain.',
    'confirm.restore': 'Go back to official OpenAI?\nYour third-party providers stay configured; you can switch back any time.',
    'prompt.quota': 'Enter this plan’s total token allowance (e.g. 500000000).\nLeave empty to clear it.\n\nYou provide the number; the tool only uses it to show a local usage percentage and never guesses.',
    'prompt.model': 'Enter model names, comma-separated:',

    'repair.preview_title': 'Dry run: {count} tasks would be repaired',
    'repair.fixed': 'Repaired {count} tasks — restart Codex to apply',
    'repair.nothing': 'Nothing to repair',

    'preset.balance_badge': ' (balance API)',
    'form.need_base_url': 'Base URL is required.',
    'form.fetching': 'Fetching models…',

    'footer.repo': 'GitHub ↗',
    'footer.help': 'If something breaks run {doctor}; stop the background service with {stop}',
    'modal.title': 'Add a provider',
    'modal.hint': 'A name and a Base URL are enough. The API key goes into your system keychain, never into the Codex config file. Local models (Ollama / LM Studio) need no key.',
    'modal.preset': 'Start from a preset (optional)',
    'modal.preset_none': '— fill it in myself —',
    'modal.label': 'Provider name *',
    'modal.label_ph': 'e.g. My gateway',
    'modal.base_url': 'Base URL *',
    'modal.models_url': 'Models endpoint (blank = infer)',
    'modal.key': 'API key (stored in keychain)',
    'modal.transport': 'Connection',
    'modal.models': 'Model list (one per line; blank = fetch automatically)',
    'modal.console_url': 'Provider website (optional, for balance)',
    'modal.balance_url': 'Balance endpoint (optional)',
    'modal.balance_path': 'Balance field path',
    'modal.currency': 'Currency',
    'modal.needs_key': 'This provider needs an API key',
  },
};

const LANG_KEY = 'codex-switcher-lang';

function detectLang() {
  // 1) 地址栏优先：?lang=zh / ?lang=en。截图脚本和分享链接靠它固定语言。
  try {
    const asked = new URLSearchParams(location.search).get('lang');
    if (asked === 'zh' || asked === 'en') {
      try { localStorage.setItem(LANG_KEY, asked); } catch (e) { /* 忽略 */ }
      return asked;
    }
  } catch (e) { /* 老浏览器没有 URLSearchParams */ }
  // 2) 上次选的
  try {
    const saved = localStorage.getItem(LANG_KEY);
    if (saved === 'zh' || saved === 'en') return saved;
  } catch (e) { /* 隐私模式下 localStorage 可能不可用 */ }
  // 3) 浏览器语言
  const nav = (navigator.language || navigator.userLanguage || '').toLowerCase();
  return nav.indexOf('zh') === 0 ? 'zh' : 'en';
}

let LANG = detectLang();

function t(key, vars) {
  const table = I18N[LANG] || I18N.zh;
  let text = table[key];
  if (text === undefined) text = (I18N.zh[key] !== undefined ? I18N.zh[key] : key);
  if (vars) {
    Object.keys(vars).forEach(function (name) {
      text = text.split('{' + name + '}').join(String(vars[name]));
    });
  }
  return text;
}

/** 把带 data-i18n* 的静态节点翻译一遍。切换语言时再调一次即可。 */
function applyI18n(root) {
  const scope = root || document;
  scope.querySelectorAll('[data-i18n]').forEach(function (node) {
    node.textContent = t(node.getAttribute('data-i18n'));
  });
  scope.querySelectorAll('[data-i18n-placeholder]').forEach(function (node) {
    node.setAttribute('placeholder', t(node.getAttribute('data-i18n-placeholder')));
  });
  scope.querySelectorAll('[data-i18n-title]').forEach(function (node) {
    node.setAttribute('title', t(node.getAttribute('data-i18n-title')));
  });
  document.documentElement.setAttribute('lang', LANG === 'zh' ? 'zh-CN' : 'en');
  document.title = t('app.title');
  const toggle = document.getElementById('btn-lang');
  if (toggle) toggle.textContent = t('lang.other');
}

function setLang(lang) {
  LANG = (lang === 'en') ? 'en' : 'zh';
  try { localStorage.setItem(LANG_KEY, LANG); } catch (e) { /* 忽略 */ }
  applyI18n();
  window.dispatchEvent(new CustomEvent('langchange'));
}

function toggleLang() {
  setLang(LANG === 'zh' ? 'en' : 'zh');
}
