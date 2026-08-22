"""滑块的反检测内核选择与 Chromium 复用。

回归两件事：
1. 引擎固定为原生 playwright —— patchright 已弃用（它阉割 add_init_script，
   隐身脚本从未生效）；webdriver 标志由 --disable-blink-features=
   AutomationControlled + 隐身脚本注入处理。
2. Chromium 可执行文件要能从现成安装里解析出来，避免重复下载。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import xianyu_slider_stealth as slider


class ChromiumDiscoveryTests(unittest.TestCase):
    def _make_install(self, root: Path, revision: str, layout: str) -> Path:
        executable = root / f"chromium-{revision}" / layout
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\n")
        return executable

    def test_finds_linux_chromium_from_browsers_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = self._make_install(root, "1223", "chrome-linux/chrome")

            with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(root)}):
                found = slider.find_chromium_executable()

            self.assertEqual(found, str(expected))

    def test_prefers_newest_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_install(root, "1100", "chrome-linux/chrome")
            newest = self._make_install(root, "1223", "chrome-linux/chrome")

            with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(root)}):
                found = slider.find_chromium_executable()

            self.assertEqual(found, str(newest))

    def test_ignores_headless_shell_only_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # headless shell 是单独下载的包，缺少滑块页面需要的渲染能力
            shell = root / "chromium_headless_shell-1223" / "chrome-linux" / "headless_shell"
            shell.parent.mkdir(parents=True, exist_ok=True)
            shell.write_text("#!/bin/sh\n")

            with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(root)}):
                found = slider.find_chromium_executable()

            self.assertIsNone(found)

    def test_missing_install_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": tmp}):
                # 目录存在但没装浏览器时要老实返回 None，让调用方走默认解析
                self.assertIsNone(slider.find_chromium_executable())


class StealthEngineSelectionTests(unittest.TestCase):
    def _make_instance(self):
        instance = slider.XianyuSliderStealth.__new__(slider.XianyuSliderStealth)
        instance.user_id = instance.pure_user_id = "acc"
        instance._browser_slot_held = False
        instance.headless = True
        instance.page = instance.context = instance.browser = instance.playwright = None
        return instance

    def test_playwright_is_the_only_engine(self):
        # patchright 已弃用（阉割 add_init_script，隐身脚本从未生效）
        instance = self._make_instance()
        instance._stealth_engine = "playwright"
        self.assertEqual(instance._stealth_engine, "playwright")
        self.assertFalse(slider.PATCHRIGHT_AVAILABLE)

    def test_engine_marker_is_set_before_launch(self):
        # 回退分支的日志会读这个字段，不能等到 init_browser 才有值
        with patch.object(slider.XianyuSliderStealth, "_check_date_validity", return_value=True):
            instance = slider.XianyuSliderStealth(user_id="acc", enable_learning=False)
        try:
            self.assertEqual(instance._stealth_engine, "playwright")
        finally:
            slider.concurrency_manager.unregister_instance(instance.user_id)

    def test_patchright_is_removed_from_requirements(self):
        text = Path("requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("patchright", text)


if __name__ == "__main__":
    unittest.main()
