import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "commit_private_vault.py"

SPEC = importlib.util.spec_from_file_location("commit_private_vault_path_guard", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class IndexPathCompatibilityTests(unittest.TestCase):
    def check_index_path(self, name, blocked):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            subprocess.run(["git", "init", str(vault)], check=True, capture_output=True)
            blob = subprocess.check_output(["git", "-C", str(vault), "hash-object", "-w", "--stdin"], input=b"fixture\n").decode().strip()
            # Index-only paths reproduce Linux checkout failures without asking
            # the host filesystem to create an overlong component.
            subprocess.run(["git", "-C", str(vault), "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}"], check=True, capture_output=True)
            with contextlib.redirect_stderr(io.StringIO()) as output:
                if blocked:
                    with self.assertRaises(SystemExit) as failure:
                        MODULE.require_compatible_index_paths(vault)
                    self.assertEqual(failure.exception.code, 2)
                else:
                    MODULE.require_compatible_index_paths(vault)
            self.assertNotIn(name, output.getvalue())
            if blocked:
                self.assertIn("paths suppressed", output.getvalue())

    def test_chinese_component_over_255_bytes_is_blocked(self):
        self.check_index_path("Raw/" + "私" * 85 + ".md", True)

    def test_chinese_component_exactly_255_bytes_is_allowed(self):
        self.check_index_path("Raw/" + "私" * 84 + ".md", False)

    def test_overlong_directory_component_is_also_blocked(self):
        self.check_index_path("私" * 86 + "/note.md", True)


class CommitPrivateVaultTests(unittest.TestCase):
    def test_changed_file_path_is_not_printed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            seed = root / "seed"
            vault = root / "vault"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.invalid"], check=True)
            (seed / "README").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "seed"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)
            subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
            subprocess.run(["git", "clone", str(remote), str(vault)], check=True, capture_output=True)

            private_path = vault / "Raw" / "secret-title.md"
            private_path.parent.mkdir()
            private_path.write_text("private\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--vault", str(vault), "--message", "safe", "--path", "Raw"],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("secret-title", result.stdout + result.stderr)
            self.assertNotIn(str(private_path), result.stdout + result.stderr)
            self.assertIn("changed=yes", result.stdout)


if __name__ == "__main__":
    unittest.main()
