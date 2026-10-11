import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "commit_private_vault.py"


class SourcePublicationTests(unittest.TestCase):
    def test_concurrent_writer_is_rejected_without_force_push(self):
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

            # Another writer advances main after this checkout: no force push or overwrite.
            (seed / "README").write_text("concurrent change\n")
            subprocess.run(["git", "-C", str(seed), "pull", "--ff-only", "origin", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "concurrent"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)
            expected = subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse", "main"])
            private_path.write_text("second sync\n")
            conflict = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(vault), "--message", "safe", "--path", "Raw"], capture_output=True, text=True)
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("ancestry check failed", conflict.stderr)
            self.assertEqual(expected, subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse", "main"]))


if __name__ == "__main__":
    unittest.main()
