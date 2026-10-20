const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'overlay.js'), 'utf8');
function extract(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, 'missing block ' + start);
  return source.slice(a, b);
}
/* html() 的完整渲染链：额度芯片 + token 明细折叠进 tooltip。
 * 依赖的 ico/fmt/ttX 等是纯展示辅助，这里给最小实现——测的是连线，不是配色。 */
const block = (() => {
  const a = source.indexOf('    function providerFromDom(q) {');
  const marker = '\n    }\n\n    /* 当前会话识别';
  const b = source.indexOf(marker, a);
  assert.ok(a >= 0 && b > a, 'missing html block');
  // marker 以 `}` 开头，要把它一起带上，否则函数体不完整
  return source.slice(a, b + marker.indexOf('\n', 1));
})();

function render(d, { show = { win: 1, ctx: 1, today: 1, turn: 1, sub: 1, tools: 1, quota: 1 }, lang = 'zh', shrink = false } = {}) {
  const ctx = {
    Date,
    L: (zh, en) => (lang === 'en' ? en : zh),
    ttH: t => '<div class="rt-h">' + t + '</div>',
    ttRow: (k, v) => '<div class="rt-row"><b>' + k + '</b><span>' + v + '</span></div>',
    ttNote: v => '<div class="rt-note">' + v + '</div>',
    ttEsc: s => String(s),
    ico: () => '<svg></svg>',
    fmt: n => String(n == null ? 0 : n),
    cachePct: () => '',
    sec: ms => (ms ? (ms / 1000).toFixed(1) + 's' : '–'),
    fmtTime: () => '00:00',
    fmtStart: ms => (ms ? '2026-10-20 10:00' : '?'),
    ioc: () => '',
    excActive: () => false,
    zuBarSpeedView: () => null,
    zuIcoThinking: () => '',
    state: { show, lang, ctxOv: '', showQuota: 1 },
    liveStatsCur: null,
    shrink,
    qProv: null,
  };
  vm.createContext(ctx);
  vm.runInContext(block, ctx);
  return { out: vm.runInContext('html(' + JSON.stringify(d) + ')', ctx), ctx };
}

const EMPTY_TURN = { requests: 0, retries: 0, tool_calls: 0, tool_errors: 0, input: 0, output: 0,
  reasoning: 0, cache_read: 0, cache_write: 0, total: 0, duration_ms: 0, ttft_ms: 0, started: 0, ended: 0, tps: 0 };

function payload(providerId, quota) {
  return {
    session: { sid: 'sess_x', total: 5000, input: 4000, output: 1000, cache_read: 3000, reasoning: 0,
      cache_write: 0, turns: 3, requests: 9, tool_calls: 2, retries: 0, ctx: 12000, started: 1, last_at: 2 },
    last_turn: Object.assign({}, EMPTY_TURN, { total: 800, requests: 2, input: 700, output: 100, cache_read: 500 }),
    last: { provider: providerId, model: 'm', duration_ms: 3000, ttft_ms: 400, tps: 40 },
    today: { total: 9000, input: 8000, output: 1000, cache_read: 6000, reasoning: 0, requests: 20, retries: 0, started: 1, last_at: 2 },
    tools: { total: 2, errors: 0, list: [] },
    context_window: 100000,
    quota,
  };
}

const CLINE = {
  provider_id: 'new-provider-2', name: 'clinepass', kind: 'clinepass',
  credential_status: 'valid', stale: false, plan_name: 'Cline Pass (Monthly)',
  windows: [
    { key: 'five_hour', label: '5小时', used_percent: 6, resets_at: 0 },
    { key: 'weekly', label: '周', used_percent: 48, resets_at: 0 },
    { key: 'monthly', label: '月', used_percent: 24, resets_at: 0 },
  ],
};

test('bar renders one chip per quota window', () => {
  const { out } = render(payload('new-provider-2', { 'new-provider-2': CLINE }));
  assert.match(out.s, /5小时/);
  assert.match(out.s, />6%</);
  assert.match(out.s, />48%</);
  assert.match(out.s, />24%</);
  assert.equal((out.s.match(/zu-q"/g) || []).length, 3);
});

test('quota chip tooltip stays focused on quota windows', () => {
  const { out } = render(payload('new-provider-2', { 'new-provider-2': CLINE }));
  assert.equal(out.tips[0] !== null, true);
  const tip = out.tips[0];
  assert.match(tip, /额度占用 · clinepass/);
  assert.match(tip, /5小时/);
  assert.match(tip, />6%</);
  assert.match(tip, />48%</);
});

test('token totals stay on the bar as their own items', () => {
  const { out } = render(payload('new-provider-2', { 'new-provider-2': CLINE }));
  // 会话累计与今日合计是独立条项，不折进额度 tooltip
  assert.equal(out.s.includes('会话'), true);
  assert.equal(out.s.includes('今日'), true);
  assert.equal(out.tips.filter(Boolean).length >= 2, true);   // 会话 + 今日
});

test('provider without quota keeps the classic bar', () => {
  const { out } = render(payload('account:bigmodel-individual-coding-plan', { 'new-provider-2': CLINE }));
  assert.equal(out.s.includes('zu-q'), false);
  // 老布局回归：token 项回到条面并各自带 tooltip
  assert.equal(out.tips.filter(Boolean).length >= 2, true);   // 会话 + 今日
});

test('quota section can be switched off from settings', () => {
  const { out } = render(payload('new-provider-2', { 'new-provider-2': CLINE }),
    { show: { win: 1, today: 1, quota: 0, ctx: 0, turn: 0, sub: 0, tools: 0 } });
  assert.equal(out.s.includes('zu-q'), false);
  assert.equal(out.s.includes('5小时'), false);
});

test('switching models switches which quota is shown', () => {
  const minimax = { provider_id: 'minimax', name: 'MiniMax', kind: 'minimax',
    credential_status: 'valid', stale: false,
    windows: [{ key: 'five_hour', label: '5小时', used_percent: 100, resets_at: 0 }] };
  const q = { minimax, 'new-provider-2': CLINE };
  const a = render(payload('minimax', q));
  assert.equal(a.out.s.includes('>100%<'), true);
  assert.equal((a.out.s.match(/zu-q"/g) || []).length, 1);
  const b = render(payload('new-provider-2', q));
  assert.equal((b.out.s.match(/zu-q"/g) || []).length, 3);
});

test('expired credential renders no chip and no quota tooltip', () => {
  const expired = Object.assign({}, CLINE, { credential_status: 'expired', windows: [] });
  const { out } = render(payload('new-provider-2', { 'new-provider-2': expired }));
  assert.equal(out.s.includes('zu-q'), false);
});

test('chips are localized', () => {
  const mm = { provider_id: 'minimax', name: 'MiniMax', kind: 'minimax',
    credential_status: 'valid', stale: false,
    windows: [{ key: 'five_hour', label: '5小时', used_percent: 62, resets_at: Date.now() + 3600000 }] };
  const { out } = render(payload('minimax', { minimax: mm }), { lang: 'en' });
  assert.match(out.tips[0], /Quota used · MiniMax/);
  assert.match(out.tips[0], /resets in/);
});

test('balance-only provider renders a credit chip instead of windows', () => {
  const deepseek = {
    provider_id: 'deepseek', name: 'DeepSeek', kind: 'deepseek',
    credential_status: 'valid', stale: false,
    windows: [], balance: { amount: 12.34, currency: 'CNY', detail: '赠送 2.00 / 充值 10.34' },
  };
  const { out } = render(payload('deepseek', { deepseek }));
  assert.match(out.s, /余额/);
  assert.match(out.s, /¥12\.34/);
  assert.equal(out.s.includes('zu-q'), true);
  assert.equal(out.tips[0], null === out.tips[0] ? null : out.tips[0]);  // 有 tooltip
});

test('provider with neither windows nor balance renders nothing', () => {
  const empty = { provider_id: 'xiaomi-mimo', name: 'MiMo', kind: 'mimo',
    credential_status: 'valid', stale: false, windows: [], balance: null };
  const { out } = render(payload('xiaomi-mimo', { 'xiaomi-mimo': empty }));
  assert.equal(out.s.includes('余额'), false);
  assert.equal(out.s.includes('zu-q'), false);
});
