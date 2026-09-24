import re
import unittest
from pathlib import Path

from codex_cleanup_tool.version import APP_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    def test_release_version_is_130(self):
        self.assertEqual(APP_VERSION, "1.3.0")

    def test_windows_ci_builds_and_verifies_with_python_314(self):
        workflow = (PROJECT_ROOT / ".github/workflows/windows-tests.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('python-version: "3.14"', workflow)
        self.assertIn("actions/checkout@v7", workflow)
        self.assertIn("actions/setup-python@v7", workflow)
        self.assertIn("requirements-build.txt", workflow)
        self.assertIn("build_cleanup_package.ps1", workflow)
        self.assertIn("tests.test_package_contents", workflow)

    def test_build_dependency_is_pinned(self):
        requirements = (PROJECT_ROOT / "requirements-build.txt").read_text(
            encoding="utf-8"
        )

        self.assertRegex(requirements.strip(), r"^pyinstaller==\d+\.\d+\.\d+$")

        build_script = (PROJECT_ROOT / "build_cleanup_package.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("sys.version_info[:2] == (3, 14)", build_script)

    def test_readmes_link_to_latest_release_without_fixed_archive_version(self):
        for name in ("README.md", "README.zh-CN.md"):
            with self.subTest(name=name):
                readme = (PROJECT_ROOT / name).read_text(encoding="utf-8")
                self.assertIn(
                    "https://github.com/yeh2017/codex-local-data-cleanup-tool/releases/latest",
                    readme,
                )
                self.assertIn("Python 3.14", readme)
                self.assertIsNone(
                    re.search(
                        r"codex_local_data_cleanup_tool_v\d+\.\d+\.\d+_windows_x64\.zip",
                        readme,
                    )
                )


if __name__ == "__main__":
    unittest.main()
