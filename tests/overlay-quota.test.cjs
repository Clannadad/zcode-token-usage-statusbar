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
/* 额度渲染的纯函数块：provider 选择 / 倒计时 / 档位 / 芯片 / 悬停明细 */
const quotaSource = extract('    function providerFromDom(q) {', '    function html(d) {');

function load(lang = 'zh') {
  const ctx = {
    L: (zh, en) => (lang === 'en' ? en : zh),
    ttH: t => '<div class="rt-h">' + t + '</div>',
    ttRow: (k, v) => '<div class="rt-row"><b>' + k + '</b><span>' + v + '</span></div>',
    ttNote: v => '<div class="rt-note">' + v + '</div>',
    Date,
  };
  vm.createContext(ctx);
  vm.runInContext(quotaSource, ctx);
  return ctx;
}

const CLINEPASS = {
  provider_id: 'new-provider-2', name: 'clinepass', kind: 'clinepass',
  credential_status: 'valid', stale: false, plan_name: 'Cline Pass (Monthly)', balance: 500000,
  windows: [
    { key: 'five_hour', label: '5小时', used_percent: 6, resets_at: 0 },
    { key: 'weekly', label: '周', used_percent: 48, resets_at: 0 },
    { key: 'monthly', label: '月', used_percent: 24, resets_at: 0 },
  ],
};
const MINIMAX = {
  provider_id: 'minimax', name: 'MiniMax', kind: 'minimax',
  credential_status: 'valid', stale: false,
  windows: [{ key: 'five_hour', label: '5小时', used_percent: 80, resets_at: 0 }],
};

test('quota follows the provider of the current model', () => {
  const c = load();
  const d = { last: { provider: 'new-provider-2' }, quota: { 'new-provider-2': CLINEPASS, minimax: MINIMAX } };
  assert.equal(c.quotaProvider(d).provider_id, 'new-provider-2');
  d.last.provider = 'minimax';
  assert.equal(c.quotaProvider(d).provider_id, 'minimax');
});

test('switching to a provider without quota data yields no chips', () => {
  const c = load();
  /* provider 明确但不在额度表里（切到了 GLM 等）：不显示，绝不拿别人的额度顶上 */
  const d = { last: { provider: 'account:bigmodel-individual-coding-plan' }, quota: { minimax: MINIMAX } };
  assert.equal(c.quotaProvider(d), null);
  /* provider 未知（会话还没落过请求）：唯一候选可以用 */
  const d2 = { last: {}, quota: { minimax: MINIMAX } };
  assert.equal(c.quotaProvider(d2).provider_id, 'minimax');
  /* 多个候选且 provider 未知：不猜 */
  const d3 = { last: {}, quota: { minimax: MINIMAX, 'new-provider-2': CLINEPASS } };
  assert.equal(c.quotaProvider(d3), null);
});

test('known provider with expired credentials yields no chips', () => {
  const c = load();
  const expired = Object.assign({}, MINIMAX, { credential_status: 'expired', windows: [] });
  const d = { last: { provider: 'minimax' }, quota: { minimax: expired } };
  assert.equal(c.quotaProvider(d), null);
});

test('no quota payload at all returns null', () => {
  const c = load();
  assert.equal(c.quotaProvider({ last: {} }), null);
  assert.equal(c.quotaProvider({}), null);
  assert.equal(c.quotaProvider(null), null);
});

test('usage bands: green under 70, yellow 70-85, red at or above 85', () => {
  const c = load();
  assert.equal(c.quotaCls(0), 'ok');
  assert.equal(c.quotaCls(69.9), 'ok');
  assert.equal(c.quotaCls(70), 'warm');
  assert.equal(c.quotaCls(84.9), 'warm');
  assert.equal(c.quotaCls(85), 'hot');
  assert.equal(c.quotaCls(100), 'hot');
});

test('chip renders label and rounded percentage', () => {
  const c = load();
  const html = c.quotaChipHtml({ label: '周', used_percent: 47.6 });
  assert.match(html, /class="q-w">周</);
  assert.match(html, />48%</);            // 四舍五入
  assert.match(html, /class="q-v ok"/);   // 47.6% 落在绿档
  assert.equal(html.includes('q-dot'), false);   // 没有进度圆点（条面保持文字化）
});

test('countdown formatting covers minutes, hours, days and reset', () => {
  const c = load();
  const now = Date.now();
  /* 各档留 30 秒余量：quotaCountdown 内部另取 Date.now()，整分钟边界在慢机上会掉一档 */
  assert.equal(c.quotaCountdown(now + 45.5 * 60000), '45分');
  assert.equal(c.quotaCountdown(now + (3 * 60 + 17.5) * 60000), '3小时17分');
  assert.equal(c.quotaCountdown(now + 3.5 * 60000), '3分');     // 整小时不留 "3小时0分"
  assert.equal(c.quotaCountdown(now + (2 * 24 + 5.5) * 3600000), '2天5小时');
  assert.equal(c.quotaCountdown(now - 1000), '即将重置');
  assert.equal(c.quotaCountdown(0), '');
});

test('countdown is localized', () => {
  const c = load('en');
  const now = Date.now();
  assert.equal(c.quotaCountdown(now + 45.5 * 60000), '45m');
  assert.equal(c.quotaCountdown(now + (3 * 60 + 17.5) * 60000), '3h17m');
  assert.equal(c.quotaCountdown(now + (2 * 24 + 0.5) * 3600000), '2d');
});

test('tooltip lists every window with its reset countdown', () => {
  const c = load();
  /* 留 30 分钟余量：countdown 内部另取 Date.now()，整点边界（3h00m）在慢机上会掉到 2h59m */
  const tip = c.quotaTip(Object.assign({}, CLINEPASS, {
    windows: [
      { key: 'five_hour', label: '5小时', used_percent: 6, resets_at: Date.now() + 3.5 * 3600000 },
      { key: 'weekly', label: '周', used_percent: 48, resets_at: Date.now() + 30 * 3600000 },
      { key: 'monthly', label: '月', used_percent: 24, resets_at: 0 },
    ],
  }));
  assert.match(tip, /额度占用 · clinepass/);
  assert.match(tip, /Cline Pass \(Monthly\)/);
  assert.match(tip, /5小时/);
  assert.match(tip, /重置 3小时/);
  assert.match(tip, /周/);
  assert.match(tip, /账户余额/);
  assert.match(tip, /500000/);
});

test('stale provider says so instead of silently showing old numbers', () => {
  const c = load();
  const tip = c.quotaTip(Object.assign({}, MINIMAX, { stale: true, error: 'network error: timeout' }));
  assert.match(tip, /查询失败/);
  assert.match(tip, /network error/);
});

test('tooltip is localized', () => {
  const c = load('en');
  const tip = c.quotaTip(CLINEPASS);
  assert.match(tip, /Quota used · clinepass/);
  assert.match(tip, /Credit balance/);
});
