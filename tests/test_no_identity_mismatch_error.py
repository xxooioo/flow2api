"""守护测试：任何地方都不得再产生“Flow 登录账号与当前 Token 不一致”这类硬失败提示。

真实账号以 Flow 页面返回为准，刷新时自动更正本地邮箱；
只有「该账号已被另一个 Token 占用」才会报错。
"""

import pathlib
import unittest

FORBIDDEN_SNIPPETS = (
    "与当前 Token 不一致",
    "请同步对应账号的 Google Cookies",
)

SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src"


class NoIdentityMismatchErrorTests(unittest.TestCase):
    def test_source_tree_never_contains_identity_mismatch_error(self):
        offenders = []
        for path in sorted(SRC_ROOT.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".js", ".html"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for snippet in FORBIDDEN_SNIPPETS:
                if snippet in text:
                    offenders.append(f"{path}: {snippet}")
        self.assertEqual(offenders, [], "发现禁止出现的身份不一致提示：" + "; ".join(offenders))


class LegacyRefreshResultSanitizerTests(unittest.TestCase):
    """旧库里残留的历史错误不允许再回显到前端。"""

    def setUp(self):
        from src.api import admin as admin_mod

        self.admin = admin_mod

    def legacy_text(self):
        # 拼接构造，避免源码里出现被禁止的完整提示语
        return (
            "RuntimeError: Flow 登录账号"
            + "与当前 Token 不一致，请同步对应账号的 Google Cookies"
        )

    def test_legacy_identity_error_is_dropped(self):
        # 这类历史报错不再展示：统一清洗成空串
        self.assertEqual(self.admin._sanitize_refresh_result(self.legacy_text()), "")

    def test_other_errors_are_kept(self):
        self.assertEqual(
            self.admin._sanitize_refresh_result("RuntimeError: connection timed out"),
            "RuntimeError: connection timed out",
        )
        self.assertEqual(self.admin._sanitize_refresh_result("success"), "success")
        self.assertEqual(self.admin._sanitize_refresh_result(None), "")


if __name__ == "__main__":
    unittest.main()
