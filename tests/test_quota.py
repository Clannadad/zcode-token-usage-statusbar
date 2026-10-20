# -*- coding: utf-8 -*-
"""quota.py 适配器解析回归：覆盖全部供应商的响应结构。

全部离线——只测纯函数，不发网络请求、不读用户真实 provider 配置。
响应体按各家公开/实测结构构造（2026-10 采集）。
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
import quota  # noqa: E402


class RouteTests(unittest.TestCase):
    def test_domain_routes(self):
        cases = {
            "https://api.minimax.cn/v1": "minimax",
            "https://api.minimaxi.com/anthropic": "minimax",
            "https://api.cline.bot/api/v1": "clinepass",
            "https://api.deepseek.com": "deepseek",
            "https://api.moonshot.cn/v1": "moonshot",
            "https://api.kimi.com/coding": "kimi",
            "https://open.bigmodel.cn/api/anthropic": "glm",
            "https://api.z.ai/api/paas/v4": "glm",
            "https://opencode.ai/zen/go/v1": "opencode",
            "https://api.commandcode.ai": "commandcode",
            "https://api.stepfun.com/v1": "stepfun",
            "https://api.xiaomimimo.com/v1": "mimo",
            "https://api.siliconflow.cn/v1": "siliconflow",
            "https://openrouter.ai/api/v1": "openrouter",
            "https://api.novita.ai/openapi": "novita",
            "https://ollama.com/api": "ollama",
        }
        for url, kind in cases.items():
            self.assertEqual(quota.classify_provider(url), kind, url)

    def test_unknown_provider_is_not_queried(self):
        self.assertIsNone(quota.classify_provider("https://example.com/v1"))
        self.assertIsNone(quota.classify_provider(""))

    def test_id_fallback_when_base_url_missing(self):
        """ZCode 内置模板可能不在配置里写 baseUrl，只能靠 providerId 推断。"""
        self.assertEqual(quota.classify_by_id("xiaomi-mimo", "Xiaomi MiMo"), "mimo")
        self.assertEqual(quota.classify_by_id("minimax", "MiniMax"), "minimax")
        self.assertEqual(quota.classify_by_id("kimi", "Kimi"), "kimi")
        self.assertEqual(quota.classify_by_id("new-provider-2", "clinepass"), "clinepass")
        self.assertIsNone(quota.classify_by_id("new-provider-3", "某供应商"))  # 无信息时不猜


class MinimaxParseTests(unittest.TestCase):
    BODY = {
        "model_remains": [
            {"model_name": "general", "end_time": 1792479600000,
             "current_interval_remaining_percent": 38,
             "current_weekly_status": 3, "current_weekly_remaining_percent": 100},
            {"model_name": "video", "current_interval_remaining_percent": 100},
        ],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }

    def test_remaining_inverted(self):
        windows = quota.parse_minimax(self.BODY)
        self.assertEqual([w["key"] for w in windows], ["five_hour"])
        self.assertEqual(windows[0]["used_percent"], 62.0)

    def test_no_weekly_plan_omits_window(self):
        self.assertNotIn("weekly", [w["key"] for w in quota.parse_minimax(self.BODY)])

    def test_active_weekly_included(self):
        body = {"model_remains": [{"model_name": "general", "end_time": 1,
                                   "current_interval_remaining_percent": 10,
                                   "current_weekly_status": 1,
                                   "current_weekly_remaining_percent": 85,
                                   "weekly_end_time": 2}]}
        by_key = {w["key"]: w for w in quota.parse_minimax(body)}
        self.assertEqual(by_key["weekly"]["used_percent"], 15.0)
        self.assertEqual(by_key["weekly"]["resets_at"], 2)

    def test_business_error_raises(self):
        with self.assertRaises(ValueError):
            quota.parse_minimax({"base_resp": {"status_code": 1004, "status_msg": "login fail"}})


class ClinepassParseTests(unittest.TestCase):
    BODY = {"data": {"limits": [
        {"type": "five_hour", "percentUsed": 6, "resetsAt": "2026-10-20T05:32:19.824411366Z"},
        {"type": "weekly", "percentUsed": 48, "resetsAt": "2026-10-21T12:46:44.82724865Z"},
        {"type": "monthly", "percentUsed": 24},
    ]}, "success": True}

    def test_three_windows(self):
        windows = quota.parse_clinepass(self.BODY)
        self.assertEqual([w["key"] for w in windows], ["five_hour", "weekly", "monthly"])
        self.assertEqual([w["used_percent"] for w in windows], [6.0, 48.0, 24.0])

    def test_nanosecond_timestamps(self):
        for w in quota.parse_clinepass(self.BODY)[:2]:
            self.assertGreater(w["resets_at"], 1700000000000)


class KimiParseTests(unittest.TestCase):
    BODY = {
        "limits": [{"detail": {"limit": 1000, "remaining": 500,
                               "resetTime": "2026-05-24T12:00:00Z"}}],
        "usage": {"limit": 5000, "remaining": 3000, "resetTime": "2026-05-30T00:00:00Z"},
    }

    def test_windows_computed_from_limit_remaining(self):
        by_key = {w["key"]: w for w in quota.parse_kimi(self.BODY)}
        self.assertEqual(by_key["five_hour"]["used_percent"], 50.0)
        self.assertEqual(by_key["weekly"]["used_percent"], 40.0)
        self.assertGreater(by_key["weekly"]["resets_at"], 0)

    def test_monthly_pool(self):
        body = dict(self.BODY)
        body["limit_month_total"] = {"limit": 100, "remaining": 25, "resetTime": "2026-06-01T00:00:00Z"}
        by_key = {w["key"]: w for w in quota.parse_kimi(body)}
        self.assertEqual(by_key["monthly"]["used_percent"], 75.0)

    def test_zero_limit_is_skipped(self):
        body = {"limits": [{"detail": {"limit": 0, "remaining": 0}}]}
        self.assertEqual(quota.parse_kimi(body), [])


class GlmParseTests(unittest.TestCase):
    BODY = {"code": 200, "success": True, "data": {
        "limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "usage": 800000000,
             "currentValue": 127694464, "percentage": 15, "nextResetTime": 1770648402389},
            {"type": "TIME_LIMIT", "percentage": 45},
        ],
        "level": "pro",
    }}

    def test_token_window(self):
        windows = quota.parse_glm(self.BODY)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["key"], "five_hour")
        self.assertEqual(windows[0]["used_percent"], 15.0)
        self.assertEqual(windows[0]["resets_at"], 1770648402389)

    def test_time_limit_is_ignored(self):
        self.assertEqual([w["key"] for w in quota.parse_glm(self.BODY)], ["five_hour"])

    def test_level(self):
        self.assertEqual(quota._glm_level(self.BODY), "pro")

    def test_business_error_raises(self):
        with self.assertRaises(ValueError):
            quota.parse_glm({"success": False, "msg": "no subscription"})


class OpencodeParseTests(unittest.TestCase):
    BODY = {"usage": {
        "rolling": {"status": "ok", "percent": 11, "resetsAt": "2026-08-17T17:10:09.580Z"},
        "weekly": {"status": "ok", "percent": 4, "resetsAt": "2026-08-24T00:00:00.580Z"},
        "monthly": {"status": "ok", "percent": 2, "resetsAt": "2026-09-13T04:26:00.580Z"},
    }}

    def test_three_windows(self):
        windows = quota.parse_opencode(self.BODY)
        self.assertEqual([w["key"] for w in windows], ["five_hour", "weekly", "monthly"])
        self.assertEqual([w["used_percent"] for w in windows], [11.0, 4.0, 2.0])

    def test_missing_window_skipped(self):
        body = {"usage": {"rolling": {"percent": 5}}}
        self.assertEqual([w["key"] for w in quota.parse_opencode(body)], ["five_hour"])


class CommandCodeParseTests(unittest.TestCase):
    def test_window_limits_and_credits(self):
        subs = {"data": {"windowLimits": {
            "fiveHour": {"used": 2, "limit": 10, "resetsAt": "2026-10-20T10:00:00Z"},
            "weekly": {"used": 30, "limit": 100},
        }}}
        credits = {"data": {"remaining": 12.5}}
        windows, credit = quota.parse_commandcode(credits, subs)
        by_key = {w["key"]: w for w in windows}
        self.assertEqual(by_key["five_hour"]["used_percent"], 20.0)
        self.assertEqual(by_key["weekly"]["used_percent"], 30.0)
        self.assertEqual(credit, 12.5)

    def test_empty_is_tolerated(self):
        windows, credit = quota.parse_commandcode({}, {})
        self.assertEqual(windows, [])
        self.assertIsNone(credit)


class BalanceParseTests(unittest.TestCase):
    def test_deepseek(self):
        body = {"is_available": True, "balance_infos": [
            {"currency": "USD", "total_balance": "1.00"},
            {"currency": "CNY", "total_balance": "100.50", "granted_balance": "50.00",
             "topped_up_balance": "50.50"},
        ]}
        with mock.patch.object(quota, "_http_json", return_value=(200, body, None)):
            out = quota.query_deepseek({"api_key": "k"})
        self.assertEqual(out["balance"]["amount"], 100.5)
        self.assertEqual(out["balance"]["currency"], "CNY")   # 优先人民币账户

    def test_deepseek_unauthorized(self):
        with mock.patch.object(quota, "_http_json", return_value=(401, {}, None)):
            with self.assertRaises(PermissionError):
                quota.query_deepseek({"api_key": "k"})

    def test_moonshot(self):
        body = {"data": {"available_balance": 42.75}}
        with mock.patch.object(quota, "_http_json", return_value=(200, body, None)):
            out = quota.query_moonshot({"api_key": "k", "base_url": "https://api.moonshot.cn/v1"})
        self.assertEqual(out["balance"]["amount"], 42.75)
        self.assertEqual(out["balance"]["currency"], "CNY")

    def test_siliconflow(self):
        body = {"data": {"totalBalance": "8.5"}}
        with mock.patch.object(quota, "_http_json", return_value=(200, body, None)):
            out = quota.query_siliconflow({"api_key": "k", "base_url": "https://api.siliconflow.cn/v1"})
        self.assertEqual(out["balance"]["amount"], 8.5)

    def test_stepfun(self):
        body = {"data": {"balance": "12.34"}}
        with mock.patch.object(quota, "_http_json", return_value=(200, body, None)):
            out = quota.query_stepfun({"api_key": "k"})
        self.assertEqual(out["balance"]["amount"], 12.34)
        self.assertEqual(out["balance"]["currency"], "CNY")

    def test_openrouter_subtracts_usage(self):
        body = {"data": {"total_credits": 100, "total_usage": 35.5}}
        with mock.patch.object(quota, "_http_json", return_value=(200, body, None)):
            out = quota.query_openrouter({"api_key": "k"})
        self.assertEqual(out["balance"]["amount"], 64.5)
        self.assertEqual(out["balance"]["currency"], "USD")

    def test_ollama_string_balance(self):
        with mock.patch.object(quota, "_http_json", return_value=(200, {"balance": "7.25"}, None)):
            out = quota.query_ollama({"api_key": "k"})
        self.assertEqual(out["balance"]["amount"], 7.25)

    def test_mimo_reports_no_api(self):
        """MiMo 只有模型接口可用，余额需要网页 Cookie——必须诚实报告而不是编数字。"""
        with mock.patch.object(quota, "_http_json", return_value=(200, {"data": []}, None)):
            with self.assertRaises(RuntimeError) as ctx:
                quota.query_mimo({"api_key": "k"})
        self.assertIn("MiMo", str(ctx.exception))


class CacheAndIsolationTests(unittest.TestCase):
    def setUp(self):
        quota._CACHE.clear()

    def _providers(self):
        return {"minimax": {"provider_id": "minimax", "name": "MiniMax", "kind": "minimax",
                            "api_key": "k", "base_url": "https://api.minimax.cn/v1", "models": ["MiniMax-M3"]}}

    def test_cache_hit_avoids_second_call(self):
        calls = []

        def fake(provider, timeout=15):
            calls.append(1)
            return {"windows": [quota._window("five_hour", 10.0)]}

        with mock.patch.object(quota, "load_providers", return_value=self._providers()), \
                mock.patch.object(quota, "query_minimax", side_effect=fake):
            quota.get_quota(poll_ms=60000)
            quota.get_quota(poll_ms=60000)
        self.assertEqual(len(calls), 1)

    def test_failure_marks_stale_and_keeps_last(self):
        state = {"n": 0}

        def flaky(provider, timeout=15):
            state["n"] += 1
            if state["n"] == 1:
                return {"windows": [quota._window("five_hour", 10.0)]}
            raise RuntimeError("boom")

        with mock.patch.object(quota, "load_providers", return_value=self._providers()), \
                mock.patch.object(quota, "query_minimax", side_effect=flaky):
            quota.get_quota(poll_ms=0)
            out = quota.get_quota(poll_ms=0)
        self.assertTrue(out["minimax"]["stale"])
        self.assertEqual(out["minimax"]["windows"][0]["used_percent"], 10.0)

    def test_auth_failure_marks_expired(self):
        with mock.patch.object(quota, "load_providers", return_value=self._providers()), \
                mock.patch.object(quota, "query_minimax", side_effect=PermissionError("HTTP 401")):
            out = quota.get_quota(poll_ms=0)
        self.assertEqual(out["minimax"]["credential_status"], "expired")

    def test_one_failure_does_not_hide_another(self):
        providers = dict(self._providers())
        providers["deepseek"] = {"provider_id": "deepseek", "name": "DeepSeek", "kind": "deepseek",
                                 "api_key": "k2", "base_url": "https://api.deepseek.com", "models": []}
        with mock.patch.object(quota, "load_providers", return_value=providers), \
                mock.patch.object(quota, "query_minimax", side_effect=RuntimeError("down")), \
                mock.patch.object(quota, "query_deepseek",
                                  return_value={"windows": [], "balance": {"amount": 5.0, "currency": "CNY"}}):
            out = quota.get_quota(poll_ms=0)
        self.assertEqual(out["deepseek"]["balance"]["amount"], 5.0)
        self.assertEqual(out["minimax"]["windows"], [])

    def test_disabled_returns_empty(self):
        self.assertEqual(quota.get_quota(enabled=False), {})

    def test_provider_without_key_skipped(self):
        p = self._providers()
        p["minimax"]["api_key"] = ""
        with mock.patch.object(quota, "load_providers", return_value=p):
            self.assertEqual(quota.get_quota(poll_ms=0), {})


class CliContractTests(unittest.TestCase):
    def test_single_json_line(self):
        import contextlib
        import io
        out = io.StringIO()
        with mock.patch.object(quota, "get_quota", return_value={"x": {"name": "n", "windows": []}}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(quota.main([]), 0)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)
        self.assertEqual(json.loads(out.getvalue())["x"]["name"], "n")

    def test_provider_filter(self):
        seen = {}

        def fake(provider_ids=None, **kw):
            seen["ids"] = provider_ids
            return {}

        with mock.patch.object(quota, "get_quota", side_effect=fake):
            quota.main(["--provider", "minimax"])
        self.assertEqual(seen["ids"], ["minimax"])


if __name__ == "__main__":
    unittest.main()
