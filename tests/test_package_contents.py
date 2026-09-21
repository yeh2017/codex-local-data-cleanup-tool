import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = (
    PROJECT_ROOT.parent
    / "outputs"
    / "chatgpt_codex_local_history_cleanup_tool_windows_x64"
)
EXECUTABLE = PACKAGE_ROOT / "ChatGPT-Codex Local History Cleanup Tool.exe"


class PackageContentsTests(unittest.TestCase):
    def test_build_script_creates_windowed_onedir_application(self):
        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "build_cleanup_package.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("PyInstaller", script)
        self.assertIn("--onedir", script)
        self.assertIn("--windowed", script)
        self.assertIn("--icon", script)
        self.assertIn("ChatGPT-Codex Local History Cleanup Tool", script)
        self.assertIn("chatgpt_codex_local_history_cleanup_tool_windows_x64", script)
        self.assertLess(
            script.index("Remove-Item -LiteralPath $zipPath"),
            script.index("-m PyInstaller"),
        )

    @unittest.skipUnless(EXECUTABLE.is_file(), "需要先构建 Windows 可执行包")
    def test_built_package_is_independent_folder(self):
        package_root = PACKAGE_ROOT

        self.assertTrue(
            (package_root / "ChatGPT-Codex Local History Cleanup Tool.exe").is_file()
        )
        self.assertFalse((package_root / "Codex 本地记录清理工具.exe").exists())
        self.assertFalse((package_root / "CodexLocalCleanupTool.exe").exists())
        self.assertTrue((package_root / "_internal").is_dir())
        self.assertTrue((package_root / "diagnose_codex_cleanup_tool.bat").is_file())
        self.assertFalse(any(package_root.rglob("*.py")))
        self.assertFalse(any(package_root.rglob("*.pyc")))
        self.assertFalse((package_root / "start_codex_cleanup_tool.vbs").exists())
        self.assertFalse(any(package_root.glob("README_*.md")))
        self.assertFalse((package_root / "cleanup_tool_settings.json").exists())

    def test_diagnostic_launcher_uses_bundled_executable(self):
        project_root = Path(__file__).resolve().parents[1]
        launcher = (project_root / "diagnose_codex_cleanup_tool.bat").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("chcp 65001", launcher)
        self.assertIn("ChatGPT-Codex Local History Cleanup Tool.exe", launcher)
        self.assertIn("--startup-check", launcher)
        self.assertIn("startup.log", launcher)
        self.assertIn("pause", launcher.lower())
        self.assertNotIn("python", launcher.lower())

    def test_launcher_uses_windows_crlf_line_endings(self):
        project_root = Path(__file__).resolve().parents[1]
        launcher = (project_root / "diagnose_codex_cleanup_tool.bat").read_bytes()

        self.assertIn(b"\r\n", launcher)
        self.assertNotIn(b"\n", launcher.replace(b"\r\n", b""))

    @unittest.skipUnless(EXECUTABLE.is_file(), "需要先构建 Windows 可执行包")
    def test_built_executable_passes_startup_check(self):
        executable = EXECUTABLE

        result = subprocess.run(
            [str(executable), "--startup-check"],
            cwd=executable.parent,
            timeout=30,
            check=False,
        )

        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
