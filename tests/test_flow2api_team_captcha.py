"""Flow2API 团队打码（内置 yes 协议服务）回归测试。

服务地址与密钥写死在代码中，不读取任何用户配置/数据库字段；
请求格式与 YesCaptcha 一致（createTask / getTaskResult）。
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.config import (
    FLOW2API_TEAM_CAPTCHA_BASE_URL,
    FLOW2API_TEAM_CAPTCHA_CLIENT_KEY,
    FLOW2API_TEAM_CAPTCHA_METHOD,
    FLOW2API_TEAM_CAPTCHA_TASK_TYPE,
    normalize_captcha_method,
)
from src.services.flow_client import FlowClient


PROXY = "http://user:pass@host:10000"


class _FakeProxyManager:
    async def get_request_proxy_url(self):
        return PROXY


class _FakeYesStyleSession:
    """createTask -> taskId, getTaskResult -> ready（记录所有请求）。"""

    def __init__(self):
        self.calls = 0
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls += 1
        self.requests.append((url, kwargs))
        response = MagicMock()
        response.status_code = 200
        if self.calls == 1:
            response.json.return_value = {"errorId": 0, "taskId": "team-task-1"}
        else:
            response.json.return_value = {
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "gRecaptchaResponse": "team-token",
                    "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140",
                },
            }
        return response


class TeamCaptchaConstantsTests(unittest.TestCase):
    def test_endpoint_and_key_are_hardcoded(self):
        self.assertEqual(FLOW2API_TEAM_CAPTCHA_METHOD, "flow2api_team")
        self.assertEqual(FLOW2API_TEAM_CAPTCHA_BASE_URL, "https://captcha.haqimi.uk")
        self.assertEqual(
            FLOW2API_TEAM_CAPTCHA_CLIENT_KEY, "sk-flow-mint-2f9a7c1e4b8d6350"
        )
        self.assertEqual(
            FLOW2API_TEAM_CAPTCHA_TASK_TYPE, "RecaptchaV3TaskProxylessM1S9"
        )

    def test_method_aliases_normalize(self):
        for alias in ("flow2api_team", "flow2api-team", "FLOW2API_TEAM", " flow2api-team "):
            with self.subTest(alias=alias):
                self.assertEqual(
                    normalize_captcha_method(alias), FLOW2API_TEAM_CAPTCHA_METHOD
                )

    def test_user_yescaptcha_values_are_not_reused_for_team_mode(self):
        self.assertNotEqual(
            FLOW2API_TEAM_CAPTCHA_BASE_URL, "https://api.yescaptcha.com"
        )
        self.assertTrue(FLOW2API_TEAM_CAPTCHA_CLIENT_KEY.startswith("sk-flow-mint-"))


class TeamCaptchaSolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_flow_client_uses_builtin_endpoint_and_yes_payload(self):
        flow = FlowClient.__new__(FlowClient)
        flow.proxy_manager = _FakeProxyManager()
        session = _FakeYesStyleSession()
        flow._build_team_captcha_cookie_map = AsyncMock(
            return_value={"SID": "sid-value", "OSID": "osid-value"}
        )
        with (
            patch("src.services.flow_client.AsyncSession", lambda *a, **kw: session),
            patch("src.services.flow_client.config") as cfg,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            # 用户自己的 YesCaptcha 配置必须完全被忽略
            cfg.yescaptcha_api_key = "user-key"
            cfg.yescaptcha_base_url = "https://api.yescaptcha.com"
            cfg.yescaptcha_task_type = "RecaptchaV3TaskProxyless"
            cfg.debug_enabled = False

            result = await flow._get_api_captcha_token(
                method=FLOW2API_TEAM_CAPTCHA_METHOD,
                project_id="proj-1",
                action="IMAGE_GENERATION",
            )

        self.assertIsNotNone(result)
        token, user_agent = result
        self.assertEqual(token, "team-token")
        self.assertIn("Windows", user_agent)

        create_url, create_kwargs = session.requests[0]
        self.assertTrue(create_url.startswith(FLOW2API_TEAM_CAPTCHA_BASE_URL))
        self.assertTrue(create_url.endswith("/createTask"))
        payload = create_kwargs["json"]
        self.assertEqual(payload["clientKey"], FLOW2API_TEAM_CAPTCHA_CLIENT_KEY)
        self.assertEqual(payload["task"]["proxy"], PROXY)
        self.assertEqual(payload["softID"], "33424")
        self.assertEqual(payload["task"]["type"], FLOW2API_TEAM_CAPTCHA_TASK_TYPE)
        self.assertEqual(payload["task"]["minScore"], 0.9)
        self.assertEqual(payload["task"]["pageAction"], "IMAGE_GENERATION")
        self.assertEqual(
            payload["task"]["cookie"], {"SID": "sid-value", "OSID": "osid-value"}
        )

        get_url, get_kwargs = session.requests[-1]
        self.assertTrue(get_url.endswith("/getTaskResult"))
        self.assertEqual(get_kwargs["json"]["clientKey"], FLOW2API_TEAM_CAPTCHA_CLIENT_KEY)
        self.assertEqual(get_kwargs["json"]["taskId"], "team-task-1")

    async def test_admin_score_test_helper_uses_builtin_endpoint(self):
        from src.api import admin as admin_mod

        session = _FakeYesStyleSession()
        with (
            patch("src.api.admin.AsyncSession", lambda *a, **kw: session),
            patch("src.api.admin.config") as cfg,
            patch("src.api.admin.proxy_manager", None),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            cfg.yescaptcha_api_key = "user-key"
            cfg.yescaptcha_base_url = "https://api.yescaptcha.com"
            token = await admin_mod._solve_recaptcha_with_api_service(
                method=FLOW2API_TEAM_CAPTCHA_METHOD,
                website_url="https://flow.google.com/projects",
                website_key="6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
                action="IMAGE_GENERATION",
            )

        self.assertEqual(token, "team-token")
        create_url, create_kwargs = session.requests[0]
        self.assertTrue(create_url.startswith(FLOW2API_TEAM_CAPTCHA_BASE_URL))
        self.assertEqual(
            create_kwargs["json"]["clientKey"], FLOW2API_TEAM_CAPTCHA_CLIENT_KEY
        )

    async def test_third_party_resolver_prefers_team_mode(self):
        flow = FlowClient.__new__(FlowClient)
        with patch("src.services.flow_client.config") as cfg:
            cfg.captcha_method = FLOW2API_TEAM_CAPTCHA_METHOD
            cfg.yescaptcha_api_key = ""
            self.assertEqual(
                flow._resolve_third_party_captcha_method(),
                FLOW2API_TEAM_CAPTCHA_METHOD,
            )

    async def test_team_cookie_map_uses_flow_request_cookie_pairs(self):
        flow = FlowClient.__new__(FlowClient)
        storage = (
            '[{"name":"OSID","value":"osid-1","domain":"flow.google.com",'
            '"hostOnly":true,"path":"/","secure":true},'
            '{"name":"SID","value":"sid-1","domain":".google.com","path":"/",'
            '"secure":true}]'
        )
        flow._resolve_flow_frontend_cookie_storage = AsyncMock(return_value=storage)
        cookie_map = await flow._build_team_captcha_cookie_map(7)
        self.assertEqual(cookie_map, {"OSID": "osid-1", "SID": "sid-1"})

    async def test_team_cookie_map_is_empty_without_cookies(self):
        flow = FlowClient.__new__(FlowClient)
        flow._resolve_flow_frontend_cookie_storage = AsyncMock(return_value="")
        self.assertEqual(await flow._build_team_captcha_cookie_map(None), {})

    async def test_supported_api_method_set_includes_team(self):
        from src.api import admin as admin_mod

        self.assertIn(FLOW2API_TEAM_CAPTCHA_METHOD, admin_mod.SUPPORTED_API_CAPTCHA_METHODS)


if __name__ == "__main__":
    unittest.main()