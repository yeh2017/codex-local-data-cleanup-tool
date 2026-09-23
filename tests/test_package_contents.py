import subprocess
import unittest
import hashlib
import shutil
import tempfile
from pathlib import Path

from codex_cleanup_tool.version import APP_EXECUTABLE_NAME, APP_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = (
    PROJECT_ROOT.parent
    / "outputs"
    / f"codex_local_data_cleanup_tool_v{APP_VERSION}_windows_x64"
)
EXECUTABLE = PACKAGE_ROOT / f"{APP_EXECUTABLE_NAME}.exe"


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
        self.assertIn("--version-file", script)
        self.assertIn("APP_EXECUTABLE_NAME", script)
        self.assertIn("codex_local_data_cleanup_tool_v", script)
        self.assertIn("[IO.Path]::GetTempPath()", script)
        self.assertIn("codex-cleanup-build-", script)
        self.assertLess(
            script.index("Remove-Item -LiteralPath $zipPath"),
            script.index("-m PyInstaller"),
        )

    @unittest.skipUnless(EXECUTABLE.is_file(), "需要先构建 Windows 可执行包")
    def test_built_package_is_independent_folder(self):
        package_root = PACKAGE_ROOT

        self.assertTrue(
            (package_root / f"{APP_EXECUTABLE_NAME}.exe").is_file()
        )
        self.assertFalse((package_root / "Codex 本地记录清理工具.exe").exists())
        self.assertFalse((package_root / "CodexLocalCleanupTool.exe").exists())
        self.assertTrue((package_root / "_internal").is_dir())
        self.assertTrue((package_root / "diagnose_codex_cleanup_tool.bat").is_file())
        self.assertTrue((package_root / "README.md").is_file())
        self.assertTrue((package_root / "README.zh-CN.md").is_file())
        self.assertTrue((package_root / "LICENSE").is_file())
        self.assertFalse(any(package_root.rglob("*.py")))
        self.assertFalse(any(package_root.rglob("*.pyc")))
        self.assertFalse((package_root / "start_codex_cleanup_tool.vbs").exists())
        self.assertFalse((package_root / "cleanup_tool_settings.json").exists())

        zip_path = Path(str(package_root) + ".zip")
        checksum_path = Path(str(zip_path) + ".sha256")
        digest = hashlib.sha256(zip_path.read_bytes()).hexdigest().upper()
        self.assertEqual(
            checksum_path.read_text(encoding="ascii").strip(),
            f"{digest}  {zip_path.name}",
        )

    def test_diagnostic_launcher_uses_bundled_executable(self):
        project_root = Path(__file__).resolve().parents[1]
        launcher = (project_root / "diagnose_codex_cleanup_tool.bat").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("chcp 65001", launcher)
        self.assertIn(f"{APP_EXECUTABLE_NAME}.exe", launcher)
        self.assertIn("--startup-check", launcher)
        self.assertIn("startup.log", launcher)
        self.assertIn("pause", launcher.lower())
        self.assertNotIn("python", launcher.lower())

    def test_diagnostic_launcher_does_not_execute_directory_metacharacters(self):
        with tempfile.TemporaryDirectory() as temporary:
            malicious = Path(temporary) / "package&mkdir PWNED&echo"
            malicious.mkdir()
            launcher = malicious / "diagnose_codex_cleanup_tool.bat"
            shutil.copy2(PROJECT_ROOT / launcher.name, launcher)

            subprocess.run(
                f'cmd.exe /d /s /c ""{launcher}""',
                cwd=malicious,
                input="\n",
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=15,
                check=False,
            )

            self.assertFalse((malicious / "PWNED").exists())

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
