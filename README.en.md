<div align="center">

# ZCode Quota Status Bar

**How much quota the current model has left, and when it resets — at a glance.**

A live status bar for [ZCode](https://zcode.z.ai) chat: provider quota for the model you're using, plus generation speed and token usage.

[简体中文](README.md) · English

Windows / macOS / Linux · Python 3.8+ · [MIT](LICENSE)

![ZCode quota status bar](docs/hero.png)

</div>

## What it does

- **Quota at a glance**: the 5-hour / weekly / monthly windows for the current model, colored green → yellow → red as usage climbs.
- **Reset countdown**: hover any window to see how long until it resets (`3d 10h`).
- **Follows your model**: switching models switches the provider immediately, without waiting for the next request to be recorded.
- **Speed**: generation tok/s (thinking included) and first-token latency.
- **Usage**: session token total and request count, plus today's total.
- **Pay-as-you-go too**: accounts without plan windows (e.g. DeepSeek) show the remaining balance instead.
- **Local first**: token data comes from the local SQLite database and never leaves the machine; only quota lookups reach the network.

## Supported providers

### Plan windows (5-hour / weekly / monthly)

| Provider | Shows | Endpoint |
|---|---|---|
| **MiniMax Token Plan** | 5-hour window; weekly too when the plan has one | `api.minimaxi.com/v1/api/openplatform/coding_plan/remains` |
| **ClinePass** | 5-hour / weekly / monthly + plan name + balance | `api.cline.bot/api/v1/users/me/plan/usage-limits` |
| **Kimi** | 5-hour / weekly (monthly pool when present) | `api.kimi.com/coding/v1/usages` |
| **GLM / Z.ai** | 5-hour token window + plan level | `open.bigmodel.cn/api/monitor/usage/quota/limit` |
| **OpenCode Go** | rolling / weekly / monthly | `opencode.ai/zen/go/v1/usage` |
| **Command Code** | 5-hour / weekly + remaining credit | `api.commandcode.ai/alpha/billing/*` |

### Account balance (pay-as-you-go)

| Provider | Endpoint |
|---|---|
| **DeepSeek** | `api.deepseek.com/user/balance` |
| **Moonshot** | `api.moonshot.cn|.ai/v1/users/me/balance` |
| **SiliconFlow** | `api.siliconflow.cn/v1/user/info` |
| **StepFun** | `api.stepfun.com/v1/accounts` |
| **OpenRouter** | `openrouter.ai/api/v1/credits` |
| **Novita** | `api.novita.ai/openapi/v1/billing/balance/detail` |
| **Ollama Cloud** | `ollama.com/api/balance` |

**Xiaomi MiMo**: only the model list is reachable with an API key; balance and plan usage require a signed-in web console cookie. The plugin detects the provider and shows nothing rather than inventing a number.

Any provider not in these tables triggers no request at all — the bar simply keeps its normal appearance.

## Install

### 1. From the plugin marketplace (recommended)

1. Open ZCode **Settings → Plugins → Marketplace → Add → Add plugin marketplace**
2. Paste the repository:

```text
Clannadad/zcode-token-usage-statusbar
```

3. Find **ZCode Token Usage Statusbar** under the **Personal** tab and install it
4. **Quit ZCode completely and reopen it** — closing the window is not enough

> The bar needs a small loader injected into the client (this modifies `app.asar`). It only appears after a full restart. Uninstalling removes just that injection and never rolls back the client version.

### 2. From a local directory

Clone the repository, then in **Add plugin marketplace** choose **Select directory** and pick the repository root (the folder containing `marketplace.json`).

### 3. One-shot installer (without the plugin system)

```bash
python install.py --root "D:\Zoey\ZCode"
```

`--root` points at the ZCode install directory (the one containing `resources/app.asar`). The script copies runtime files, registers the MCP server and injects the bar. If the asar is locked by a running client, a watcher window finishes the swap once you quit ZCode.

## Configuration

The runtime config lives at `~/.zcode/zcode-token-usage-statusbar/config.json`. **Change only the fields you need.**

```json
{
  "quota": { "enabled": true, "poll_ms": 60000, "timeout_s": 15 },
  "activity_min_ms": 1500,
  "heartbeat_ms": 30000,
  "hot_reload": true,
  "answer_rate_line": true
}
```

| Field | Meaning |
|---|---|
| `quota.enabled` | Quota lookup switch, default `true`. Set `false` for a purely local, offline bar |
| `quota.poll_ms` | Quota refresh interval, default 60000 (1 min). Quota moves slowly |
| `quota.timeout_s` | Per-request quota timeout, default 15 s |
| `activity_min_ms` | Minimum interval between token queries while active, default 1500 ms |
| `heartbeat_ms` | Fallback poll when file events are missed, default 30000 ms |
| `answer_rate_line` | Append a rate line to each reply, on by default |

⚠️ JSON does not allow comments — merge only the keys you need.

## Privacy

- **Token data**: reads `~/.zcode/cli/db/db.sqlite` locally. No network, nothing uploaded.
- **Quota data**: uses the API key already stored in ZCode's own `provider_config.json` — the same key you configured in the client, nothing to re-enter.
- **Keys never leave the query process**: not written to snapshots, not sent to the renderer, not logged, not uploaded.
- **The only outbound calls** are quota lookups: they send the API key and nothing else. Disable entirely with `quota.enabled: false`.
- Plugin updates download code from the configured source — a separate network path.

## How it works

```
ZCode main process (injected inject-main.cjs)
   ├── zusage.py serve   ← reads local SQLite for token usage
   └── quota.py          ← reads provider config, asks each provider for quota
        ↓ merged into one snapshot
   overlay.js (the bar inside the renderer)
        ↓ picks the provider for the current model
   5h 7%   week 52%   month 24%   |   ⚡ 44 t/s   |   session 108M / 441 req   |   today 303M
```

- Quota is a slow variable: it runs in its own process with a 60-second cache so it never slows the frequent token refresh.
- Provider selection prefers **the model currently shown in the UI**; only if that is unavailable does it fall back to the most recent request in the database.
- If a provider changes its API shape, that provider alone shows as unavailable (hover for the reason); the rest of the bar keeps working.

## Troubleshooting

**The bar doesn't show up.** Keep the plugin enabled, open a new session to trigger injection, then **fully quit ZCode** (including the tray icon). If injection failed, run `/install-overlay`.

**Quota never appears.** Check the `quota` field in `diag-0.json` inside the runtime directory — it records the selected provider, how many windows were returned, and the failure reason.

**Quota didn't change after switching models.** The plugin reads the model name shown in the UI. If the UI did not actually switch (requests still go to the old provider), the bar honestly keeps showing the old quota.

**The bar disappeared after a ZCode update.** Client updates overwrite the injection. Keep the plugin enabled and open a new session to re-inject, then restart once more.

## Development

```bash
python3 sync_plugin.py --check                                  # plugin copy vs. root sources
python3 -B -m unittest discover -s tests -p 'test_*.py'         # Python regression
node --test tests/*.test.cjs                                    # Node regression
node --check overlay.js && node --check inject-main.cjs          # syntax
```

- Every `quota.py` adapter is a pure parser plus one HTTP call, covered by offline fixtures in `tests/test_quota.py` — no real keys needed.
- After editing root sources, run `python3 sync_plugin.py`.

## Uninstall

1. Turn off auto-update in ⚙
2. Run `/uninstall-overlay` (keeps the anti-reinjection marker)
3. **Settings → Plugins → Installed → Uninstall**
4. Quit and reopen ZCode completely

## Credits

Built on [ByteRax/zcode-token-usage-statusbar](https://github.com/ByteRax/zcode-token-usage-statusbar) (MIT). This fork adds provider quota, a slimmer default bar, and local marketplace distribution.

Provider endpoint shapes were cross-checked against [cc-switch](https://github.com/farion1231/cc-switch), [CodexBar](https://github.com/steipete/CodexBar) and [ai-usagebar](https://github.com/akitaonrails/ai-usagebar).

[MIT License](LICENSE)
