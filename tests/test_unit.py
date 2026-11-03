"""Offline tests: serializer, merge rules, git driver fallback, installer."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from uv_lock_merge.cli import main, merge_texts  # noqa: E402
from uv_lock_merge.merge import Unresolved, merge_locks  # noqa: E402
from uv_lock_merge.pep440 import version_key  # noqa: E402
from uv_lock_merge.serialize import detect_style, dumps, inline  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
REG = {"registry": "https://pypi.org/simple"}


def pkg(name, version, deps=(), **extra):
    p = {"name": name, "version": version, "source": dict(REG)}
    if deps:
        p["dependencies"] = [{"name": d} for d in deps]
    p.update(extra)
    return p


def root(deps):
    return {
        "name": "demo",
        "version": "0.1.0",
        "source": {"virtual": "."},
        "dependencies": [{"name": d} for d in deps],
        "metadata": {"requires-dist": [{"name": d} for d in deps]},
    }


def lock(*packages, **header):
    out = {"version": 1, "revision": 3, "requires-python": ">=3.11"}
    out.update(header)
    out["package"] = sorted(packages, key=lambda p: (p["name"], version_key(p["version"])))
    return out


def names(result):
    return [(p["name"], p["version"]) for p in result["package"]]


class Serializer(unittest.TestCase):
    def test_round_trip_fixture(self):
        text = (FIXTURES / "fork.lock").read_text()
        self.assertEqual(dumps(tomllib.loads(text), detect_style(text)), text)

    def test_round_trip_through_merge(self):
        text = (FIXTURES / "fork.lock").read_text()
        merged, notes = merge_texts(text, text, text)
        self.assertEqual(merged, text)
        self.assertEqual(notes, [])

    def test_strings_follow_toml_edit(self):
        self.assertEqual(inline("plain"), '"plain"')
        self.assertEqual(inline("python_version < '3.12'"), "\"python_version < '3.12'\"")
        self.assertEqual(inline("a\\b"), "'a\\b'")
        self.assertEqual(inline("it's\\x"), '"it\'s\\\\x"')
        self.assertEqual(inline('q"q'), '"q\\"q"')
        self.assertEqual(inline("t\tn\n"), '"t\\tn\\n"')

    def test_length_dependent_arrays(self):
        one = {"version": 1, "package": [{"name": "a", "version": "1", "source": {"virtual": "."},
                                          "metadata": {"requires-dist": [{"name": "x"}]}}]}
        self.assertIn('requires-dist = [{ name = "x" }]\n', dumps(one))
        one["package"][0]["metadata"]["requires-dist"].append({"name": "y"})
        self.assertIn('requires-dist = [\n    { name = "x" },\n    { name = "y" },\n]\n', dumps(one))

    def test_old_layout_details_are_kept(self):
        text = (
            'version = 1\nrevision = 2\nrequires-python = ">=3.11"\n\n'
            '[options]\nexclude-newer = "2026-01-01T00:00:00Z"\nexclude-newer-span = "P7D"\n\n'
            '[manifest]\n\n[manifest.dependency-groups]\ndev = [{ name = "x" }]\n'
        )
        self.assertEqual(dumps(tomllib.loads(text), detect_style(text)), text)


class Merge(unittest.TestCase):
    def test_both_add_packages(self):
        base = lock(root(["a"]), pkg("a", "1"))
        ours = lock(root(["a", "b"]), pkg("a", "1"), pkg("b", "1"))
        theirs = lock(root(["a", "c"]), pkg("a", "1"), pkg("c", "2"))
        merged, notes = merge_locks(base, ours, theirs)
        self.assertEqual(names(merged), [("a", "1"), ("b", "1"), ("c", "2"), ("demo", "0.1.0")])
        demo = merged["package"][-1]
        self.assertEqual([d["name"] for d in demo["dependencies"]], ["a", "b", "c"])
        self.assertEqual(notes, [])

    def test_one_side_upgrade_wins_over_unchanged(self):
        base = lock(root(["a"]), pkg("a", "1"))
        ours = lock(root(["a"]), pkg("a", "2"))
        theirs = lock(root(["a", "b"]), pkg("a", "1"), pkg("b", "1"))
        merged, _ = merge_locks(base, ours, theirs)
        self.assertEqual(names(merged), [("a", "2"), ("b", "1"), ("demo", "0.1.0")])

    def test_conflicting_upgrades_keep_the_higher_version(self):
        base = lock(root(["a"]), pkg("a", "1.0"))
        ours = lock(root(["a"]), pkg("a", "1.10"))
        theirs = lock(root(["a"]), pkg("a", "1.9"))
        merged, notes = merge_locks(base, ours, theirs)
        self.assertEqual(names(merged), [("a", "1.10"), ("demo", "0.1.0")])
        self.assertEqual(notes, ["a: both sides changed the version; kept 1.10 over 1.9"])

    def test_package_still_needed_is_restored_and_orphans_dropped(self):
        # ours drops `req` (and its dependency `cert`); theirs adds `hx`, which needs `cert`.
        base = lock(root(["req"]), pkg("req", "1", ["cert", "u3"]), pkg("cert", "1"), pkg("u3", "1"))
        ours = lock(root([]))
        theirs = lock(root(["req", "hx"]), pkg("req", "1", ["cert", "u3"]), pkg("cert", "1"),
                      pkg("u3", "1"), pkg("hx", "1", ["cert"]))
        merged, notes = merge_locks(base, ours, theirs)
        self.assertEqual(names(merged), [("cert", "1"), ("demo", "0.1.0"), ("hx", "1")])
        self.assertIn("cert: kept, still required after the merge", notes)

    def test_dependency_of_a_package_removed_on_both_paths_is_dropped(self):
        base = lock(root(["a"]), pkg("a", "1", ["x"]), pkg("x", "1"))
        ours = lock(root(["a"]), pkg("a", "2"))          # a 2 no longer needs x
        theirs = lock(root(["a", "b"]), pkg("a", "1", ["x"]), pkg("x", "1"), pkg("b", "1"))
        merged, _ = merge_locks(base, ours, theirs)
        self.assertEqual(names(merged), [("a", "2"), ("b", "1"), ("demo", "0.1.0")])

    def test_requires_python_conflict_is_unresolved(self):
        base = lock(root([]))
        ours = lock(root([]), **{"requires-python": ">=3.12"})
        theirs = lock(root([]), **{"requires-python": ">=3.13"})
        with self.assertRaisesRegex(Unresolved, "requires-python"):
            merge_locks(base, ours, theirs)

    def test_revision_takes_the_maximum(self):
        base = lock(root([]))
        ours = lock(root([]), revision=4)
        theirs = lock(root([]), revision=5)
        merged, _ = merge_locks(base, ours, theirs)
        self.assertEqual(merged["revision"], 5)

    def test_unsupported_version(self):
        with self.assertRaisesRegex(Unresolved, "unsupported lockfile version 2"):
            merge_locks({}, {"version": 2}, {"version": 1})

    def test_add_add_without_base(self):
        ours = lock(root(["a"]), pkg("a", "1"))
        theirs = lock(root(["b"]), pkg("b", "1"))
        merged, _ = merge_locks({}, ours, theirs)
        self.assertEqual(names(merged), [("a", "1"), ("b", "1"), ("demo", "0.1.0")])

    def test_fork_collapse_strips_edge_identity(self):
        text = (FIXTURES / "fork.lock").read_text()
        base = tomllib.loads(text)
        # Theirs drops the attrs<23 fork: one attrs version remains.
        theirs = copy.deepcopy(base)
        theirs["package"] = [p for p in theirs["package"] if (p["name"], p["version"]) != ("attrs", "22.2.0")]
        fixture = next(p for p in theirs["package"] if p["name"] == "fixture")
        fixture["dependencies"] = [
            d for d in fixture["dependencies"] if d.get("version") != "22.2.0"
        ]
        merged, _ = merge_locks(base, base, theirs)
        edges = next(p for p in merged["package"] if p["name"] == "fixture")["dependencies"]
        attrs = [d for d in edges if d["name"] == "attrs"]
        self.assertEqual(attrs, [{"name": "attrs", "marker": "python_full_version >= '3.12'"}])

    def test_both_sides_changing_a_fork_is_unresolved(self):
        text = (FIXTURES / "fork.lock").read_text()
        base = tomllib.loads(text)
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        for side, v in ((ours, "25.1.0"), (theirs, "25.2.0")):
            for p in side["package"]:
                if p["name"] == "attrs" and p["version"] == "25.3.0":
                    p["version"] = v
        with self.assertRaisesRegex(Unresolved, "forked versions of attrs"):
            merge_locks(base, ours, theirs)


class Versions(unittest.TestCase):
    def test_order(self):
        seq = ["1.0.dev1", "1.0a1", "1.0b2", "1.0rc1", "1.0", "1.0.post1", "1.1", "1.10", "2!0.1"]
        self.assertEqual(sorted(reversed(seq), key=version_key), seq)
        self.assertEqual(version_key("1.0"), version_key("1.0.0"))


class Git(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="uvlm-unit-"))
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.dir, check=True, env=env)
        self.old = os.getcwd()
        os.chdir(self.dir)

    def tearDown(self):
        os.chdir(self.old)

    def test_driver_falls_back_to_conflict_markers(self):
        for name, rp in (("base", ">=3.11"), ("ours", ">=3.12"), ("theirs", ">=3.13")):
            Path(name).write_text(f'version = 1\nrevision = 3\nrequires-python = "{rp}"\n')
        self.assertEqual(main(["driver", "base", "ours", "theirs", "uv.lock"]), 1)
        text = Path("ours").read_text()
        self.assertIn("<<<<<<< ours", text)
        self.assertIn(">>>>>>> theirs", text)

    def test_driver_writes_merge_result(self):
        fixture = (FIXTURES / "fork.lock").read_text()
        for name in ("base", "ours", "theirs"):
            Path(name).write_text(fixture)
        self.assertEqual(main(["driver", "base", "ours", "theirs"]), 0)
        self.assertEqual(Path("ours").read_text(), fixture)

    def test_install_is_idempotent(self):
        self.assertEqual(main(["install"]), 0)
        self.assertEqual(main(["install"]), 0)
        self.assertEqual(Path(".gitattributes").read_text(), "uv.lock merge=uv-lock\n")
        driver = subprocess.run(["git", "config", "merge.uv-lock.driver"], capture_output=True, text=True)
        self.assertIn("driver %O %A %B %P", driver.stdout)

    def test_install_local_attributes(self):
        self.assertEqual(main(["install", "--local"]), 0)
        self.assertFalse(Path(".gitattributes").exists())
        self.assertEqual(Path(".git/info/attributes").read_text(), "uv.lock merge=uv-lock\n")


if __name__ == "__main__":
    unittest.main()
