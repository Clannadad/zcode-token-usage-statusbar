# -*- coding: utf-8 -*-
"""供应商额度查询（套餐窗口占用 / 重置时间 / 账户余额）。

与 zusage.py 的分工：
- zusage.py 读本地 SQLite，回答"这个会话用了多少 token"（纯本地只读）。
- quota.py 问供应商自己的额度接口，回答"这个模型的额度还剩多少、什么时候重置"。

数据来源是 ZCode 自己的 provider 配置（provider_config.json 里的 access.apiKey），
密钥只在本进程内使用：只用于发 HTTP 请求，绝不写进快照、绝不进入渲染进程、
绝不打印到 stdout（日志走 stderr）。

两类数据：
- 窗口型（windows）：5 小时 / 周 / 月 的占用百分比 + 重置时间。
  适用于有套餐窗口的供应商（MiniMax、ClinePass、Kimi、GLM、OpenCode Go、Command Code）。
- 余额型（balance）：账户还剩多少钱。
  适用于按量计费（DeepSeek、Moonshot、SiliconFlow、StepFun、MiMo、OpenRouter、Novita）。

用法：
    python quota.py                      # 单行 JSON：{"<provider_id>": {...}, ...}
    python quota.py --provider minimax   # 只查某个 provider
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 客户端 setting.json 里的 provider 配置位置（与 zusage.py 的 HOME 基准一致）
_DESKTOP_SETTING_SUBPATH = (".zcode", "v2", "setting.json")
_PROVIDER_CONFIG_SUBPATH = (".zcode", "v2", "provider_config.json")

# 额度窗口的统一语义：key 用于排序与去重，label 是展示用的短名
WINDOW_LABELS = {
    "five_hour": "5小时",
    "weekly": "周",
    "monthly": "月",
    "credits": "额度",
}
WINDOW_ORDER = ("five_hour", "weekly", "monthly", "credits")

# 进程内缓存：provider_id → (取回时刻, 结果)
_CACHE = {}
# provider 配置的 mtime 签名：配置变了要重新读 key
_CONFIG_SIG = {"sig": None, "providers": {}}


# ---------- 客户端路径 ----------

def _desktop_home():
    """客户端 setting.json 的 HOME 基准（与 zusage.py 同源）。"""
    return (os.environ.get("ZCODE_DESKTOP_HOME_DIR", "").strip()
            or os.environ.get("HOME", "").strip()
            or os.environ.get("USERPROFILE", "").strip() or str(Path.home()))


def _client_roots():
    """可能存放 provider 配置的根目录，按优先级排列。

    客户端把 provider 配置放在 <dataBaseDir>/.zcode/v2/provider_config.json，
    dataBaseDir 来自 setting.json；没有 dataBaseDir 时用家目录。
    """
    roots = []
    home = Path(_desktop_home())
    setting = home.joinpath(*_DESKTOP_SETTING_SUBPATH)
    data_dir = ""
    try:
        data_dir = (json.loads(setting.read_text(encoding="utf-8")).get("dataBaseDir") or "").strip()
    except Exception:
        data_dir = ""
    if data_dir:
        roots.append(Path(data_dir))
    roots.append(home)
    seen, out = set(), []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _provider_config_path():
    for root in _client_roots():
        p = root.joinpath(*_PROVIDER_CONFIG_SUBPATH)
        if p.is_file():
            return p
    return None


def _provider_config_sig():
    p = _provider_config_path()
    if p is None:
        return None
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# ---------- provider 识别 ----------

# (匹配子串, 适配器键)。顺序有意义：更具体的域名放前面。
_ROUTES = (
    ("minimax", "minimax"),
    ("cline.bot", "clinepass"),
    ("moonshot", "moonshot"),
    ("kimi.com", "kimi"),
    ("kimi.ai", "kimi"),
    ("bigmodel.cn", "glm"),
    ("z.ai", "glm"),
    ("opencode.ai", "opencode"),
    ("commandcode.ai", "commandcode"),
    ("deepseek.com", "deepseek"),
    ("stepfun.com", "stepfun"),
    ("stepfun.cn", "stepfun"),
    ("xiaomimimo.com", "mimo"),
    ("siliconflow.cn", "siliconflow"),
    ("siliconflow.com", "siliconflow"),
    ("openrouter.ai", "openrouter"),
    ("novita.ai", "novita"),
    ("ollama.com", "ollama"),
)


def classify_provider(base_url):
    """按 baseUrl 判定供应商适配器；不支持的返回 None（不发任何请求）。"""
    url = (base_url or "").strip().lower()
    if not url:
        return None
    for needle, kind in _ROUTES:
        if needle in url:
            return kind
    return None


# providerId / providerName 里的关键字 → 适配器。用于配置没有 baseUrl 的条目
# （ZCode 的内置模板会把地址藏在自己那边，只留 providerId 和模型清单）。
_ID_ROUTES = (
    ("minimax", "minimax"),
    ("mimo", "mimo"),
    ("xiaomi", "mimo"),
    ("kimi", "kimi"),
    ("moonshot", "moonshot"),
    ("zhipu", "glm"),
    ("bigmodel", "glm"),
    ("zai", "glm"),
    ("glm", "glm"),
    ("opencode", "opencode"),
    ("commandcode", "commandcode"),
    ("command", "commandcode"),
    ("deepseek", "deepseek"),
    ("stepfun", "stepfun"),
    ("siliconflow", "siliconflow"),
    ("openrouter", "openrouter"),
    ("novita", "novita"),
    ("ollama", "ollama"),
    ("cline", "clinepass"),
)


def classify_by_id(provider_id, provider_name):
    """没有 baseUrl 时按 providerId / 名称推断适配器。"""
    for text in (provider_id or "", provider_name or ""):
        low = str(text).lower()
        if not low:
            continue
        for needle, kind in _ID_ROUTES:
            if needle in low:
                return kind
    return None


def _iter_provider_rules(doc):
    """从 provider_config.json 里取出 provider 规则列表。"""
    if not isinstance(doc, dict):
        return []
    cfg = doc.get("config")
    if not isinstance(cfg, dict):
        return []
    rules = cfg.get("providerConfigRules")
    if not isinstance(rules, dict):
        return []
    items = rules.get("providerRules")
    return items if isinstance(items, list) else []


def load_providers():
    """读 provider 配置，返回 {provider_id: {...}}。带 mtime 缓存。"""
    sig = _provider_config_sig()
    if sig is not None and _CONFIG_SIG["sig"] == sig:
        return _CONFIG_SIG["providers"]
    providers = {}
    p = _provider_config_path()
    if p is not None:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            doc = None
        for rule in _iter_provider_rules(doc):
            if not isinstance(rule, dict):
                continue
            pid = str(rule.get("providerId") or "").strip()
            if not pid:
                continue
            conf = rule.get("config")
            conf = conf if isinstance(conf, dict) else {}
            access = conf.get("access")
            access = access if isinstance(access, dict) else {}
            api = conf.get("api")
            api = api if isinstance(api, dict) else {}
            base_url = str(api.get("baseUrl") or "").strip()
            key = str(access.get("apiKey") or "").strip()
            # 模型名清单：渲染端用界面上的当前模型文本反查 provider（切模型即时生效，
            # 不必等一次请求落库）。不含敏感信息。
            models = []
            for field in ("personalModelIds", "modelOrder"):
                seq = conf.get(field)
                if isinstance(seq, list):
                    for m in seq:
                        if isinstance(m, str) and m.strip() and m.strip() not in models:
                            models.append(m.strip())
            providers[pid] = {
                "provider_id": pid,
                "name": str(rule.get("providerName") or pid),
                "base_url": base_url,
                "api_key": key,
                "kind": classify_provider(base_url) or classify_by_id(pid, rule.get("providerName")),
                "models": models,
            }
    _CONFIG_SIG["sig"] = sig
    _CONFIG_SIG["providers"] = providers
    return providers


# ---------- HTTP ----------

def _http_json(url, api_key, timeout=15, auth="bearer"):
    """GET 一个 JSON 接口，返回 (status, parsed_body, error_text)。

    auth：
      - "bearer"：Authorization: Bearer <key>
      - "raw"：Authorization: <key>（GLM 的网关按裸值校验）
      - "none"：不带 Authorization
    """
    req = urllib.request.Request(url, method="GET")
    if auth == "bearer":
        req.add_header("Authorization", "Bearer " + api_key)
    elif auth == "raw":
        req.add_header("Authorization", api_key)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "zcode-token-usage-statusbar/1.6")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.getcode()
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        status = e.code
    except Exception as e:
        return 0, None, "network error: %s" % str(e)[:120]
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        return status, None, "bad json: %s" % str(e)[:120]
    return status, body, None


def _clamp_pct(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return max(0.0, min(100.0, f))


def _pct_from_ratio(v):
    """比率（0..1 或 0..100）→ 百分比。供应商两种口径都出现过。"""
    f = _clamp_pct(v)
    if f is None:
        return None
    if 0 < f <= 1:
        return round(f * 100.0, 1)
    return round(f, 1)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _iso_to_ms(raw):
    """ISO8601 → epoch 毫秒。带纳秒的时间戳截断到微秒（Python 3.8 兼容）。"""
    if not raw or not isinstance(raw, str):
        return 0
    text = raw.strip()
    if not text:
        return 0
    m = re.match(r"^(.*\.\d{1,6})\d*(Z|[+-]\d{2}:?\d{2})$", text)
    if m:
        text = m.group(1) + m.group(2)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ts_to_ms(raw):
    """重置时间 → 毫秒。数字（秒/毫秒）和 ISO8601 字符串都要吃：各家口径不一致。"""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return 0
        iso = _iso_to_ms(text)
        if iso:
            return iso
        raw = text
    f = _num(raw)
    if f is None or f <= 0:
        return 0
    return int(f * 1000) if f < 1e11 else int(f)


def _window(key, used_pct, resets_at=0):
    return {
        "key": key,
        "label": WINDOW_LABELS.get(key, key),
        "used_percent": used_pct,
        "resets_at": int(resets_at or 0),
        "unlimited": False,
    }


# ---------- MiniMax ----------

MINIMAX_ENDPOINTS = (
    "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains",
    "https://www.minimaxi.com/v1/token_plan/remains",
)


def parse_minimax(body):
    """MiniMax Token Plan：model_remains[] 里 model_name=="general" 是编程套餐。

    current_interval_remaining_percent 是"剩余"，已用 = 100 - 剩余；
    current_weekly_status == 1 才有周限额（==3 表示套餐无周限额，不能显示成 0%）。
    """
    windows = []
    if not isinstance(body, dict):
        return windows
    base = body.get("base_resp")
    if isinstance(base, dict):
        code = base.get("status_code")
        if code is not None and code != 0:
            raise ValueError(str(base.get("status_msg") or "minimax error %s" % code)[:120])
    items = body.get("model_remains")
    if not isinstance(items, list):
        return windows
    general = None
    for item in items:
        if isinstance(item, dict) and item.get("model_name") == "general":
            general = item
            break
    if general is None:
        return windows
    remain = _clamp_pct(general.get("current_interval_remaining_percent"))
    if remain is not None:
        windows.append(_window("five_hour", round(100.0 - remain, 1), general.get("end_time")))
    if general.get("current_weekly_status") == 1:
        remain_w = _clamp_pct(general.get("current_weekly_remaining_percent"))
        if remain_w is not None:
            windows.append(_window("weekly", round(100.0 - remain_w, 1), general.get("weekly_end_time")))
    return windows


def query_minimax(provider, timeout=15):
    last_err = None
    for url in MINIMAX_ENDPOINTS:
        status, body, err = _http_json(url, provider.get("api_key") or "", timeout)
        if err:
            last_err = err
            continue
        if status in (401, 403):
            raise PermissionError("HTTP %s" % status)
        if status and status >= 400:
            last_err = "HTTP %s" % status
            continue
        try:
            return {"windows": parse_minimax(body)}
        except ValueError as e:
            last_err = str(e)
    raise RuntimeError(last_err or "no response")


# ---------- ClinePass ----------

CLINEPASS_BASE = "https://api.cline.bot/api/v1"


def parse_clinepass(body):
    """ClinePass：data.limits[]，percentUsed 就是已用百分比。"""
    windows = []
    if not isinstance(body, dict):
        return windows
    if body.get("success") is False:
        raise ValueError(str(body.get("error") or "clinepass error")[:120])
    data = body.get("data")
    if not isinstance(data, dict):
        return windows
    limits = data.get("limits")
    if not isinstance(limits, list):
        return windows
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("type") or "").strip()
        if key not in WINDOW_ORDER:
            continue
        used = _clamp_pct(entry.get("percentUsed"))
        if used is None:
            continue
        windows.append(_window(key, round(used, 1), _iso_to_ms(entry.get("resetsAt"))))
    windows.sort(key=lambda w: WINDOW_ORDER.index(w["key"]))
    return windows


def query_clinepass(provider, timeout=15):
    api_key = provider.get("api_key") or ""
    status, body, err = _http_json(CLINEPASS_BASE + "/users/me/plan/usage-limits", api_key, timeout)
    if err:
        raise RuntimeError(err)
    if status in (401, 403):
        raise PermissionError("HTTP %s" % status)
    if status and status >= 400:
        raise RuntimeError("HTTP %s" % status)
    result = {"windows": parse_clinepass(body)}
    meta = clinepass_meta(api_key, timeout)
    result.update(meta)
    return result


def clinepass_meta(api_key, timeout=15):
    """套餐名与余额（可选信息，失败不影响额度本身）。"""
    meta = {}
    status, body, _ = _http_json(CLINEPASS_BASE + "/users/me/plan", api_key, timeout)
    if _ok(status) and isinstance(body, dict):
        data = body.get("data")
        plan = data.get("plan") if isinstance(data, dict) else None
        if isinstance(plan, dict):
            name = plan.get("displayName") or plan.get("name")
            if name:
                meta["plan_name"] = str(name)
    uid = ""
    status2, body2, _ = _http_json(CLINEPASS_BASE + "/users/me", api_key, timeout)
    if _ok(status2) and isinstance(body2, dict):
        data2 = body2.get("data")
        if isinstance(data2, dict):
            uid = str(data2.get("id") or "")
    if uid:
        status3, body3, _ = _http_json(CLINEPASS_BASE + "/users/" + uid + "/balance", api_key, timeout)
        if _ok(status3) and isinstance(body3, dict):
            data3 = body3.get("data")
            if isinstance(data3, dict) and data3.get("balance") is not None:
                meta["balance"] = data3["balance"]
    return meta


def _ok(status):
    """2xx 或 0（未取到状态）视为可用。"""
    return not status or (200 <= status < 300)


# ---------- Kimi ----------

KIMI_ENDPOINTS = (
    "https://api.kimi.com/coding/v1/usages",
    "https://api.moonshot.cn/coding/v1/usages",
)


def parse_kimi(body):
    """Kimi Coding：limits[].detail（5 小时滚动）与 usage（周/月总额）。

    实测结构：{limits:[{detail:{limit,remaining,resetTime}}], usage:{limit,remaining,resetTime}}
    新账号会返回 usages 比率映射；有 limit_month_total 时按月度池处理。
    """
    windows = []
    if not isinstance(body, dict):
        return windows

    def add(key, limit, remaining, reset, label_key=None):
        lim, rem = _num(limit), _num(remaining)
        if lim is None or rem is None or lim <= 0:
            return
        used = _clamp_pct((lim - rem) / lim * 100.0)
        if used is not None:
            windows.append(_window(label_key or key, round(used, 1), _ts_to_ms(reset)))

    limits = body.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            detail = entry.get("detail")
            if isinstance(detail, dict):
                add("five_hour", detail.get("limit"), detail.get("remaining"), detail.get("resetTime"))
    usage = body.get("usage")
    if isinstance(usage, dict):
        add("weekly", usage.get("limit"), usage.get("remaining"), usage.get("resetTime"))
    monthly = body.get("limit_month_total")
    if isinstance(monthly, dict):
        add("monthly", monthly.get("limit"), monthly.get("remaining"), monthly.get("resetTime"))
    return windows


def query_kimi(provider, timeout=15):
    api_key = provider.get("api_key") or ""
    last_err = None
    for url in KIMI_ENDPOINTS:
        status, body, err = _http_json(url, api_key, timeout)
        if err:
            last_err = err
            continue
        if status in (401, 403):
            raise PermissionError("HTTP %s" % status)
        if status and status >= 400:
            last_err = "HTTP %s" % status
            continue
        windows = parse_kimi(body)
        if windows:
            return {"windows": windows}
        last_err = "no windows in response"
    raise RuntimeError(last_err or "no response")


# ---------- GLM / Z.ai ----------

GLM_ENDPOINTS = (
    "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
    "https://api.z.ai/api/monitor/usage/quota/limit",
)


def parse_glm(body):
    """GLM Coding Plan：limits[] 里 TOKENS_LIMIT 是 token 窗口，TIME_LIMIT 是搜索次数。

    percentage 直接是已用百分比；nextResetTime 是 epoch 毫秒。
    unit=3/number=5 表示 5 小时滚动窗口。
    """
    windows = []
    if not isinstance(body, dict):
        return windows
    if body.get("success") is False:
        raise ValueError(str(body.get("msg") or "glm error")[:120])
    code = body.get("code")
    if code is not None and code not in (200, 0):
        raise ValueError(str(body.get("msg") or "glm code %s" % code)[:120])
    data = body.get("data")
    if not isinstance(data, dict):
        return windows
    limits = data.get("limits")
    if not isinstance(limits, list):
        return windows
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type") or "") != "TOKENS_LIMIT":
            continue
        used = _clamp_pct(entry.get("percentage"))
        if used is None:
            continue
        windows.append(_window("five_hour", round(used, 1), _ts_to_ms(entry.get("nextResetTime"))))
        break
    return windows


def _glm_level(body):
    data = body.get("data") if isinstance(body, dict) else None
    level = data.get("level") if isinstance(data, dict) else None
    return str(level) if level else ""


def query_glm(provider, timeout=15):
    api_key = provider.get("api_key") or ""
    last_err = None
    for url in GLM_ENDPOINTS:
        # GLM 网关对 Authorization 的两种写法都出现过，先 Bearer 再裸值
        for auth in ("bearer", "raw"):
            status, body, err = _http_json(url, api_key, timeout, auth=auth)
            if err:
                last_err = err
                continue
            if status in (401, 403):
                last_err = "HTTP %s" % status
                continue
            if status and status >= 400:
                last_err = "HTTP %s" % status
                continue
            try:
                windows = parse_glm(body)
            except ValueError as e:
                last_err = str(e)
                break
            if windows:
                result = {"windows": windows}
                level = _glm_level(body)
                if level:
                    result["plan_name"] = level
                return result
            last_err = "no windows in response"
            break
    raise RuntimeError(last_err or "no response")


# ---------- OpenCode Go ----------

OPENCODE_ENDPOINTS = ("https://opencode.ai/zen/go/v1/usage",)
_OPENCODE_WINDOWS = (("rolling", "five_hour"), ("weekly", "weekly"), ("monthly", "monthly"))


def parse_opencode(body):
    """OpenCode Go：usage.{rolling,weekly,monthly}，percent 是已用百分比（0-100）。"""
    windows = []
    if not isinstance(body, dict):
        return windows
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return windows
    for src, dst in _OPENCODE_WINDOWS:
        win = usage.get(src)
        if not isinstance(win, dict):
            continue
        pct = _clamp_pct(win.get("percent"))
        if pct is None:
            continue
        windows.append(_window(dst, round(pct, 1), _iso_to_ms(win.get("resetsAt"))))
    return windows


def query_opencode(provider, timeout=15):
    api_key = provider.get("api_key") or ""
    status, body, err = _http_json(OPENCODE_ENDPOINTS[0], api_key, timeout)
    if err:
        raise RuntimeError(err)
    if status in (401, 403):
        raise PermissionError("HTTP %s" % status)
    if status and status >= 400:
        raise RuntimeError("HTTP %s" % status)
    windows = parse_opencode(body)
    if not windows:
        raise RuntimeError("no windows in response")
    return {"windows": windows}


# ---------- Command Code ----------

COMMANDCODE_BASE = "https://api.commandcode.ai"


def parse_commandcode(credits, subscriptions):
    """Command Code：订阅给 5 小时/周 滚动花费窗口，credits 给剩余月度额度。

    两个响应都是未公开路由，按字段名而不是位置取（alpha 段随时可能变）。
    """
    windows = []
    sub = subscriptions if isinstance(subscriptions, dict) else {}
    data = sub.get("data") if isinstance(sub.get("data"), dict) else sub
    limits = data.get("windowLimits") if isinstance(data, dict) else None
    if isinstance(limits, dict):
        for src, dst in (("fiveHour", "five_hour"), ("weekly", "weekly"), ("monthly", "monthly")):
            win = limits.get(src)
            if not isinstance(win, dict):
                continue
            used = _num(win.get("used"))
            cap = _num(win.get("limit")) or _num(win.get("cap"))
            pct = _clamp_pct(used / cap * 100.0) if used is not None and cap else _clamp_pct(win.get("percent"))
            if pct is not None:
                windows.append(_window(dst, round(pct, 1), _ts_to_ms(win.get("resetsAt") or win.get("resetAt"))))
    credit_value = None
    cr = credits if isinstance(credits, dict) else {}
    cdata = cr.get("data") if isinstance(cr.get("data"), dict) else cr
    if isinstance(cdata, dict):
        remaining = _num(cdata.get("remaining"))
        if remaining is None:
            remaining = _num(cdata.get("credits"))
        if remaining is not None:
            credit_value = remaining
    return windows, credit_value


def query_commandcode(provider, timeout=15):
    api_key = provider.get("api_key") or ""
    _, credits, e1 = _http_json(COMMANDCODE_BASE + "/alpha/billing/credits", api_key, timeout)
    status2, subs, e2 = _http_json(COMMANDCODE_BASE + "/alpha/billing/subscriptions", api_key, timeout)
    if status2 in (401, 403):
        raise PermissionError("HTTP %s" % status2)
    windows, credit_val = parse_commandcode(credits, subs)
    if not windows and credit_val is None:
        raise RuntimeError(e1 or e2 or "no data in response")
    result = {"windows": windows}
    if credit_val is not None:
        result["balance"] = {"amount": credit_val, "currency": "USD"}
    return result


# ---------- 余额型 ----------

def _balance_result(amount, currency, detail=None):
    out = {"windows": [], "balance": {"amount": round(amount, 4), "currency": currency}}
    if detail:
        out["balance"]["detail"] = detail
    return out


def query_deepseek(provider, timeout=15):
    """DeepSeek：/user/balance 返回按币种分组的总额/赠送/充值。"""
    status, body, err = _http_json("https://api.deepseek.com/user/balance", provider.get("api_key") or "", timeout)
    if err:
        raise RuntimeError(err)
    if status in (401, 403):
        raise PermissionError("HTTP %s" % status)
    if status and status >= 400:
        raise RuntimeError("HTTP %s" % status)
    infos = body.get("balance_infos") if isinstance(body, dict) else None
    if not isinstance(infos, list) or not infos:
        raise RuntimeError("no balance_infos in response")
    infos_sorted = sorted(infos, key=lambda x: 0 if str(x.get("currency")) == "CNY" else 1)
    main = infos_sorted[0]
    total = _num(main.get("total_balance"))
    if total is None:
        raise RuntimeError("no total_balance")
    detail = "赠送 %s / 充值 %s" % (main.get("granted_balance"), main.get("topped_up_balance"))
    return _balance_result(total, str(main.get("currency") or "CNY"), detail)


def query_moonshot(provider, timeout=15):
    """Moonshot / Kimi 开放平台：/v1/users/me/balance。"""
    api_key = provider.get("api_key") or ""
    base = "https://api.moonshot.cn" if "cn" in (provider.get("base_url") or "") else "https://api.moonshot.ai"
    status, body, err = _http_json(base + "/v1/users/me/balance", api_key, timeout)
    if err:
        raise RuntimeError(err)
    if status in (401, 403):
        raise PermissionError("HTTP %s" % status)
    if status and status >= 400:
        raise RuntimeError("HTTP %s" % status)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("no data in response")
    amount = _num(data.get("available_balance"))
    if amount is None:
        amount = _num(data.get("voucher_balance"))
    if amount is None:
        raise RuntimeError("no balance field")
    currency = "CNY" if "cn" in base else "USD"
    return _balance_result(amount, currency)


def query_siliconflow(provider, timeout=15):
    """SiliconFlow：/v1/user/info 的 data.totalBalance / balance。"""
    api_key = provider.get("api_key") or ""
    base = "https://api.siliconflow.cn" if "cn" in (provider.get("base_url") or "") else "https://api.siliconflow.com"
    status, body, err = _http_json(base + "/v1/user/info", api_key, timeout)
    if err:
        raise RuntimeError(err)
    if status in (401, 403):
        raise PermissionError("HTTP %s" % status)
    if status and status >= 400:
        raise RuntimeError("HTTP %s" % status)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("no data in response")
    amount = _num(data.get("totalBalance"))
    if amount is None:
        amount = _num(data.get("balance"))
    if amount is None:
        raise RuntimeError("no balance field")
    currency = "CNY" if "cn" in base else "USD"
    return _balance_result(amount, currency)


def query_stepfun(provider, timeout=15):
    """StepFun：/v1/accounts 的按量余额（含现金/代金券）。"""
    status, body, err = _http_json("https://api.stepfun.com/v1/accounts", provider.get("api_key") or "", timeout)
    if err:
        raise RuntimeError(err)
    if status in (401, 403):
        raise PermissionError("HTTP %s" % status)
    if status and status >= 400:
        raise RuntimeError("HTTP %s" % status)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        data = body if isinstance(body, dict) else {}
    amount = _num(data.get("balance")) or _num(data.get("total_balance")) or _num(data.get("available_balance"))
    if amount is None:
        raise RuntimeError("no balance field")
    return _balance_result(amount, "CNY")


def query_mimo(provider, timeout=15):
    """Xiaomi MiMo：API Key 可查的只有模型列表，没有余额/套餐接口。

    实测（2026-10）：api.xiaomimimo.com/v1/models 返回 200，说明 key 有效；
    /v1/user/balance、token-plan-sgp 的同名路由都是 404；平台侧
    platform.xiaomimimo.com/api/v1/balance 需要小米账号 Cookie，不是 API Key。
    所以这里诚实地返回"无可用数据"，让状态条不显示额度，而不是编一个数字。
    """
    api_key = provider.get("api_key") or ""
    status, _, err = _http_json("https://api.xiaomimimo.com/v1/models", api_key, timeout)
    if err:
        raise RuntimeError(err)
    if status in (401, 403):
        raise PermissionError("HTTP %s" % status)
    raise RuntimeError("MiMo 无 API Key 可查的额度接口（需登录控制台）")


def query_openrouter(provider, timeout=15):
    """OpenRouter：/api/v1/credits 的 total_credits - total_usage = 剩余。"""
    status, body, err = _http_json("https://openrouter.ai/api/v1/credits", provider.get("api_key") or "", timeout)
    if err:
        raise RuntimeError(err)
    if status in (401, 403):
        raise PermissionError("HTTP %s" % status)
    if status and status >= 400:
        raise RuntimeError("HTTP %s" % status)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("no data in response")
    total = _num(data.get("total_credits"))
    used = _num(data.get("total_usage"))
    if total is None:
        raise RuntimeError("no total_credits")
    amount = total - (used or 0)
    return _balance_result(amount, "USD", "总额 %.2f / 已用 %.2f" % (total, used or 0))


def query_novita(provider, timeout=15):
    """Novita：/openapi/v1/billing/balance/detail 的 availableBalance。"""
    status, body, err = _http_json("https://api.novita.ai/openapi/v1/billing/balance/detail",
                                   provider.get("api_key") or "", timeout)
    if err:
        raise RuntimeError(err)
    if status in (401, 403):
        raise PermissionError("HTTP %s" % status)
    if status and status >= 400:
        raise RuntimeError("HTTP %s" % status)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        data = body if isinstance(body, dict) else {}
    amount = _num(data.get("availableBalance")) or _num(data.get("balance"))
    if amount is None:
        raise RuntimeError("no balance field")
    # Novita 的金额以 1e-4 为单位返回
    if amount > 1e6:
        amount = amount / 10000.0
    return _balance_result(amount, "USD")


def query_ollama(provider, timeout=15):
    """Ollama Cloud：/api/balance 的 balance 字段（字符串）。"""
    status, body, err = _http_json("https://ollama.com/api/balance", provider.get("api_key") or "", timeout)
    if err:
        raise RuntimeError(err)
    if status in (401, 403):
        raise PermissionError("HTTP %s" % status)
    if status and status >= 400:
        raise RuntimeError("HTTP %s" % status)
    text = body.get("balance") if isinstance(body, dict) else None
    amount = _num(text)
    if amount is None:
        raise RuntimeError("no balance field")
    return _balance_result(amount, "USD")


# 适配器注册表：kind → 函数名。存名字而不是函数对象，运行时再解析——
# 这样测试替换模块属性、以及将来热补丁单个适配器都能生效。
ADAPTERS = {
    "minimax": "query_minimax",
    "clinepass": "query_clinepass",
    "kimi": "query_kimi",
    "glm": "query_glm",
    "opencode": "query_opencode",
    "commandcode": "query_commandcode",
    "deepseek": "query_deepseek",
    "moonshot": "query_moonshot",
    "siliconflow": "query_siliconflow",
    "stepfun": "query_stepfun",
    "mimo": "query_mimo",
    "openrouter": "query_openrouter",
    "novita": "query_novita",
    "ollama": "query_ollama",
}


def _adapter_for(kind):
    name = ADAPTERS.get(kind)
    return globals().get(name) if name else None


# ---------- 汇总 ----------

def _query_provider(provider, timeout=15):
    kind = provider.get("kind")
    fn = _adapter_for(kind)
    if fn is None:
        return None
    data = fn(provider, timeout) or {}
    windows = data.get("windows") or []
    balance = data.get("balance")
    if not windows and not balance:
        return None
    result = {
        "provider_id": provider["provider_id"],
        "name": provider.get("name") or provider["provider_id"],
        "kind": kind,
        "models": provider.get("models") or [],
        "windows": windows,
        "credential_status": "valid",
        "stale": False,
        "queried_at": int(time.time() * 1000),
    }
    if balance:
        result["balance"] = balance
    for field in ("plan_name",):
        if data.get(field):
            result[field] = data[field]
    return result


def get_quota(provider_ids=None, timeout=15, poll_ms=60000, enabled=True):
    """返回 {provider_id: 结果}。

    每个 provider 独立成败：一个失败不影响其它（不借别人的数据）。
    失败时沿用上次成功值并标记 stale。
    """
    if not enabled:
        return {}
    providers = load_providers()
    out = {}
    now = time.time()
    for pid, provider in providers.items():
        if provider_ids and pid not in provider_ids:
            continue
        if not provider.get("kind") or not provider.get("api_key"):
            continue
        cached = _CACHE.get(pid)
        if cached and (now - cached[0]) * 1000 < poll_ms:
            out[pid] = cached[1]
            continue
        try:
            result = _query_provider(provider, timeout)
            if result is None:
                raise RuntimeError("no quota data in response")
        except PermissionError as e:
            result = {
                "provider_id": pid,
                "name": provider.get("name") or pid,
                "kind": provider.get("kind"),
                "models": provider.get("models") or [],
                "windows": [],
                "credential_status": "expired",
                "stale": False,
                "error": "认证失败 (%s)" % e,
                "queried_at": int(time.time() * 1000),
            }
            _CACHE[pid] = (now, result)
            out[pid] = result
            continue
        except Exception as e:
            if cached:
                stale = dict(cached[1])
                stale["stale"] = True
                stale["error"] = str(e)[:120]
                out[pid] = stale
            else:
                out[pid] = {
                    "provider_id": pid,
                    "name": provider.get("name") or pid,
                    "kind": provider.get("kind"),
                    "models": provider.get("models") or [],
                    "windows": [],
                    "credential_status": "unknown",
                    "stale": False,
                    "error": str(e)[:120],
                    "queried_at": int(time.time() * 1000),
                }
            continue
        _CACHE[pid] = (now, result)
        out[pid] = result
    return out


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    only = None
    for i, a in enumerate(argv):
        if a == "--provider" and i + 1 < len(argv):
            only = [argv[i + 1]]
    try:
        data = get_quota(provider_ids=only)
    except Exception as e:
        print(json.dumps({"error": str(e)[:200]}, ensure_ascii=False))
        return 0
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
