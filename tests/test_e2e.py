"""End-to-end tests with real git and real uv (needs network access to PyPI).

Each test builds a small repository, makes two branches that both change
dependencies, merges them and checks the result with `uv lock --locked`,
which fails unless the lockfile is exactly what uv would write for the
merged pyproject.toml.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
UV = shutil.which("uv")
DRIVER = f"{sys.executable} -m uv_lock_merge driver %O %A %B %P"


def env():
    e = dict(os.environ)
    e["PYTHONPATH"] = str(SRC) + os.pathsep + e.get("PYTHONPATH", "")
    e["GIT_CONFIG_NOSYSTEM"] = "1"
    e["HOME"] = e.get("UV_LOCK_MERGE_TEST_HOME", e.get("HOME", ""))
    return e


def pyproject(deps, requires_python=">=3.11", extra=""):
    lines = "".join(f'    "{d}",\n' for d in deps)
    return textwrap.dedent(
        f"""\
        [project]
        name = "demo"
        version = "0.1.0"
        requires-python = "{requires_python}"
        dependencies = [
        """
    ) + lines + "]\n" + extra


@unittest.skipUnless(UV, "uv is not installed")
class E2E(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="uvlm-"))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")
        self.git("config", "commit.gpgsign", "false")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_(self, *argv, check=True, cwd=None):
        r = subprocess.run(argv, cwd=cwd or self.dir, env=env(), capture_output=True, text=True, timeout=300)
        if check and r.returncode != 0:
            self.fail(f"{argv} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r

    def git(self, *argv, check=True):
        return self.run_("git", *argv, check=check)

    def uv(self, *argv, check=True, cwd=None):
        return self.run_(UV, *argv, check=check, cwd=cwd)

    def write(self, rel, text):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def install_driver(self):
        self.git("config", "merge.uv-lock.driver", DRIVER)
        (self.dir / ".git" / "info").mkdir(exist_ok=True)
        (self.dir / ".git" / "info" / "attributes").write_text("uv.lock merge=uv-lock\n")

    def locked_version(self, name, rel="uv.lock"):
        text = (self.dir / rel).read_text()
        m = re.search(rf'^name = "{re.escape(name)}"\nversion = "([^"]+)"', text, re.M)
        return m.group(1) if m else None

    def assert_locked(self, cwd=None):
        r = self.uv("lock", "--locked", check=False, cwd=cwd)
        self.assertEqual(r.returncode, 0, f"uv lock --locked failed:\n{r.stderr}")

    def branches(self, base, a, b):
        """base, a, b: callables that edit the work tree and run uv lock."""
        base()
        self.commit("base")
        self.git("checkout", "-q", "-b", "a")
        a()
        self.commit("a")
        self.git("checkout", "-q", "main")
        self.git("checkout", "-q", "-b", "b")
        b()
        self.commit("b")
        self.git("checkout", "-q", "a")

    def plain_merge_conflicts(self):
        r = self.git("merge", "--no-edit", "b", check=False)
        conflicted = self.git("diff", "--name-only", "--diff-filter=U").stdout.split()
        self.git("merge", "--abort", check=False)
        self.git("reset", "-q", "--hard", "a")
        return r.returncode != 0 and "uv.lock" in conflicted

    # -- scenarios ---------------------------------------------------------

    def test_both_branches_add_a_dependency(self):
        def base():
            self.write("pyproject.toml", pyproject(["httpx"]))
            self.uv("lock", "-q")

        def a():
            self.write("pyproject.toml", pyproject(["httpx", "rich"]))
            self.uv("lock", "-q")

        def b():
            self.write("pyproject.toml", pyproject(["httpx", "requests"]))
            self.uv("lock", "-q")

        self.branches(base, a, b)
        self.assertTrue(self.plain_merge_conflicts(), "expected plain git to conflict")
        self.install_driver()
        r = self.git("merge", "--no-edit", "b", check=False)
        # pyproject.toml conflicts too (same line); uv.lock must not.
        conflicted = self.git("diff", "--name-only", "--diff-filter=U").stdout.split()
        self.assertEqual(conflicted, ["pyproject.toml"], r.stdout + r.stderr)
        self.write("pyproject.toml", pyproject(["httpx", "requests", "rich"]))
        self.git("add", "pyproject.toml")
        self.assert_locked()
        self.assertIsNotNone(self.locked_version("rich"))
        self.assertIsNotNone(self.locked_version("urllib3"))

    def test_keeps_upgrades_from_both_branches(self):
        def base():
            self.write("pyproject.toml", pyproject(["httpx"]))
            self.uv("lock", "-q", "-P", "certifi==2024.2.2", "-P", "idna==3.6")

        def a():
            self.write("pyproject.toml", pyproject(["httpx", "rich"]))
            self.uv("lock", "-q", "-P", "certifi")

        def b():
            self.write("pyproject.toml", pyproject(["httpx", "requests"]))
            self.uv("lock", "-q", "-P", "idna==3.7")

        merged = pyproject(["httpx", "requests", "rich"])
        self.branches(base, a, b)
        upgraded = self.locked_version("certifi")
        self.assertNotEqual(upgraded, "2024.2.2")
        self.assertTrue(self.plain_merge_conflicts(), "expected plain git to conflict")

        # The usual workaround (take one side's uv.lock, re-run uv lock)
        # keeps that side's pins and drops the other side's upgrade.
        for side, lost, version in (("--theirs", "certifi", "2024.2.2"), ("--ours", "idna", "3.6")):
            self.git("merge", "--no-edit", "b", check=False)
            self.git("checkout", side, "uv.lock")
            self.write("pyproject.toml", merged)
            self.uv("lock", "-q")
            self.assertEqual(self.locked_version(lost), version, side)
            self.git("merge", "--abort")

        self.install_driver()
        self.git("merge", "--no-edit", "b", check=False)
        self.assertEqual(self.git("diff", "--name-only", "--diff-filter=U").stdout.split(), ["pyproject.toml"])
        self.write("pyproject.toml", merged)
        self.git("add", "pyproject.toml")
        self.assertEqual(self.locked_version("certifi"), upgraded)
        self.assertEqual(self.locked_version("idna"), "3.7")
        self.assert_locked()

    def test_removed_dependency_next_to_added_one(self):
        # a drops requests; b adds httpx, which still needs certifi and idna.
        def base():
            self.write("pyproject.toml", pyproject(["requests", "six"]))
            self.uv("lock", "-q")

        def a():
            self.write("pyproject.toml", pyproject(["six"]))
            self.uv("lock", "-q")

        def b():
            self.write("pyproject.toml", pyproject(["requests", "six", "httpx"]))
            self.uv("lock", "-q")

        self.branches(base, a, b)
        self.install_driver()
        self.git("merge", "--no-edit", "b", check=False)
        self.write("pyproject.toml", pyproject(["six", "httpx"]))
        self.git("add", "pyproject.toml")
        self.assertIsNone(self.locked_version("requests"))
        self.assertIsNone(self.locked_version("urllib3"))
        self.assertIsNotNone(self.locked_version("certifi"))
        self.assert_locked()

    def test_both_branches_upgrade_the_same_package(self):
        def base():
            self.write("pyproject.toml", pyproject(["httpx"]))
            self.uv("lock", "-q", "-P", "certifi==2024.2.2")

        def a():
            self.uv("lock", "-q", "-P", "certifi==2024.7.4")

        def b():
            self.uv("lock", "-q", "-P", "certifi==2024.8.30")

        self.branches(base, a, b)
        self.assertTrue(self.plain_merge_conflicts())
        self.install_driver()
        r = self.git("merge", "--no-edit", "b")
        self.assertIn("kept 2024.8.30 over 2024.7.4", r.stderr)
        self.assertEqual(self.locked_version("certifi"), "2024.8.30")
        self.assert_locked()

    def test_workspace_members(self):
        def member(name, deps):
            self.write(f"packages/{name}/pyproject.toml", pyproject(deps).replace('"demo"', f'"{name}"'))

        def root(extra_dep=None):
            self.write(
                "pyproject.toml",
                pyproject(["alpha", "beta"]) + textwrap.dedent(
                    """
                    [tool.uv.workspace]
                    members = ["packages/*"]

                    [tool.uv.sources]
                    alpha = { workspace = true }
                    beta = { workspace = true }
                    """
                ),
            )

        def base():
            root()
            member("alpha", ["six"])
            member("beta", ["six"])
            self.uv("lock", "-q")

        def a():
            member("alpha", ["six", "attrs"])
            self.uv("lock", "-q")

        def b():
            member("beta", ["six", "iniconfig"])
            self.uv("lock", "-q")

        self.branches(base, a, b)
        self.install_driver()
        self.git("merge", "--no-edit", "b")
        self.assertIsNotNone(self.locked_version("attrs"))
        self.assertIsNotNone(self.locked_version("iniconfig"))
        self.assert_locked()

    def test_forked_resolution(self):
        # Two attrs versions selected by Python version; both branches add a package.
        fork = ["attrs<23; python_version < '3.12'", "attrs>=23; python_version >= '3.12'"]

        def base():
            self.write("pyproject.toml", pyproject(fork, ">=3.10"))
            self.uv("lock", "-q")

        def a():
            self.write("pyproject.toml", pyproject(fork + ["six"], ">=3.10"))
            self.uv("lock", "-q")

        def b():
            self.write("pyproject.toml", pyproject(fork + ["iniconfig"], ">=3.10"))
            self.uv("lock", "-q")

        self.branches(base, a, b)
        self.assertTrue(self.plain_merge_conflicts())
        self.install_driver()
        self.git("merge", "--no-edit", "b", check=False)
        self.write("pyproject.toml", pyproject(fork + ["iniconfig", "six"], ">=3.10"))
        self.git("add", "pyproject.toml")
        self.assertEqual((self.dir / "uv.lock").read_text().count('name = "attrs"\nversion'), 2)
        self.assert_locked()

    def test_unresolvable_change_falls_back_to_a_conflict(self):
        def base():
            self.write("pyproject.toml", pyproject(["six"]))
            self.uv("lock", "-q")

        def a():
            self.write("pyproject.toml", pyproject(["six"], ">=3.12"))
            self.uv("lock", "-q")

        def b():
            self.write("pyproject.toml", pyproject(["six"], ">=3.13"))
            self.uv("lock", "-q")

        self.branches(base, a, b)
        self.install_driver()
        r = self.git("merge", "--no-edit", "b", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("both sides changed `requires-python`", r.stderr)
        self.assertIn("<<<<<<<", (self.dir / "uv.lock").read_text())

    def test_resolve_after_a_rebase_conflict(self):
        def base():
            self.write("pyproject.toml", pyproject(["httpx"]))
            self.uv("lock", "-q")

        def a():
            self.write("pyproject.toml", pyproject(["httpx", "rich"]))
            self.uv("lock", "-q")

        def b():
            self.write("pyproject.toml", pyproject(["httpx", "requests"]))
            self.uv("lock", "-q")

        self.branches(base, a, b)
        self.git("checkout", "-q", "b")
        r = self.git("rebase", "a", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.write("pyproject.toml", pyproject(["httpx", "requests", "rich"]))
        self.git("add", "pyproject.toml")
        r = self.run_(sys.executable, "-m", "uv_lock_merge", "resolve")
        self.assertIn("next: git add uv.lock", r.stderr)
        self.assert_locked()
        self.git("add", "uv.lock")
        self.run_("git", "-c", "core.editor=true", "rebase", "--continue")


if __name__ == "__main__":
    unittest.main()
