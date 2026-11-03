# uv-lock-merge

A git merge driver for `uv.lock`. When two branches both change dependencies, it
merges the lockfiles package by package, so the result keeps the versions each
branch locked, instead of leaving conflict markers in a generated file.

```
$ git merge feature-b
uv-lock-merge: merged uv.lock
Auto-merging pyproject.toml
CONFLICT (content): Merge conflict in pyproject.toml
Auto-merging uv.lock
Automatic merge failed; fix conflicts and then commit the result.
```

`pyproject.toml` still needs a person. `uv.lock` does not. After you fix
`pyproject.toml`, `uv lock --locked` confirms that the merged lockfile is
exactly what uv would write for the merged project (this is what the end to
end tests check).

## Why

uv cannot read a `uv.lock` that contains conflict markers (`error: Failed to
parse uv.lock ... invalid key`), and git conflicts on it whenever two branches
add dependencies near each other in sorted order, both touch the project's own
`dependencies` and `requires-dist` lists, or upgrade the same package.

- [astral-sh/uv#11185](https://github.com/astral-sh/uv/issues/11185) "Automatic
  resolution of merge conflicts in lockfiles" (28 reactions, open since
  02/2025). A maintainer's answer there describes the usual workaround: take
  the base branch's lockfile and run `uv lock` again.
- [astral-sh/uv#5633](https://github.com/astral-sh/uv/issues/5633) "Document
  how to resolve `uv.lock` merge conflicts" (22 reactions, open since 07/2024).
- Teams write their own drivers. A GitHub code search for `uv.lock merge=` in
  `.gitattributes` and setup scripts returned 95 repositories on 3 November
  2026 (some are forks of one project). The drivers I read take one side's
  lockfile and run `uv lock`, with descriptions such as "keep current side,
  then run uv lock" and "regenerate uv.lock from the merged pyproject.toml".
- The same problem in Poetry:
  [python-poetry/poetry#496](https://github.com/python-poetry/poetry/issues/496)
  (124 reactions, open since 2018). One commenter there describes
  sub-dependency upgrades that were invisibly downgraded when someone resolved
  the conflict by taking one side and re-locking.
- yarn, npm and Deno
  ([denoland/deno#34726](https://github.com/denoland/deno/pull/34726))
  resolve conflicts in their own lockfiles.
  uv's experimental lockfile v2
  ([#22138](https://github.com/astral-sh/uv/pull/22138), open) changes the
  format; the default format today is version 1.

Taking one side and re-locking discards the other branch's pins. The tests
show it: branch A upgrades `certifi`, branch B upgrades `idna`; `git checkout
--theirs uv.lock && uv lock` keeps the old `certifi`, `--ours` keeps the old
`idna`. uv-lock-merge keeps both.

## Install

Requires Python 3.11 or newer and git.

```sh
uv tool install git+https://github.com/Jwrede/uv-lock-merge
# or: pipx install git+https://github.com/Jwrede/uv-lock-merge
```

Register the driver in a repository:

```sh
uv-lock-merge install            # git config + a line in .gitattributes
uv-lock-merge install --local    # attribute in .git/info/attributes instead (not committed)
uv-lock-merge install --global   # all repositories: global git config and attributes file
```

This sets `merge.uv-lock.driver = uv-lock-merge driver %O %A %B %P` and adds
`uv.lock merge=uv-lock`. Git only runs merge drivers that are configured in
the local or global git config, so every clone needs `install` once; the
`.gitattributes` line alone does nothing.

## Usage

Once installed, `git merge`, `git rebase`, `git cherry-pick` and `git pull`
call the driver for every `uv.lock`, including the ones in workspace
subdirectories.

If a conflict already happened (driver not installed, or a rebase in
progress), resolve it from the index:

```sh
uv-lock-merge resolve            # merges stages 1/2/3 of uv.lock into the work tree
uv-lock-merge resolve --lock     # and runs `uv lock` if pyproject.toml has no conflict markers
git add uv.lock
```

`uv-lock-merge merge BASE OURS THEIRS` prints the merge of three files.

## Example

From `tests/test_e2e.py` (`test_keeps_upgrades_from_both_branches`), run by
hand with uv 0.13.7:

```
$ git merge feature-b   # without the driver
Auto-merging pyproject.toml
CONFLICT (content): Merge conflict in pyproject.toml
Auto-merging uv.lock
CONFLICT (content): Merge conflict in uv.lock
Automatic merge failed; fix conflicts and then commit the result.
$ git merge --abort
$ uv-lock-merge install --local
uv-lock-merge: merge driver configured; attribute in .git/info/attributes
$ git merge feature-b   # with the driver
uv-lock-merge: merged uv.lock
Auto-merging pyproject.toml
CONFLICT (content): Merge conflict in pyproject.toml
Auto-merging uv.lock
Automatic merge failed; fix conflicts and then commit the result.
$ # fix pyproject.toml, then:
$ grep -A1 -E '^name = "(certifi|idna)"' uv.lock
name = "certifi"
version = "2026.10.23"
--
name = "idna"
version = "3.7"
$ uv lock --locked
Resolved 15 packages in 2ms
```

When both branches move the same package to different versions, the driver
keeps the higher one and says so:

```
uv-lock-merge: merged uv.lock
uv-lock-merge:   certifi: both sides changed the version; kept 2024.8.30 over 2024.7.4
```

## How it works

1. Parse base, ours and theirs with `tomllib`.
2. Merge the header (`version`, `revision`, `requires-python`, markers,
   `[options]`, `[manifest]`) key by key. Lists such as workspace members and
   requirements are merged as sets: an entry added on either side is added, an
   entry removed on either side is removed.
3. Merge `[[package]]` entries keyed by name, version and source. An entry
   changed on one side takes that side's version. An entry changed on both
   sides (usually the project itself, when both branches add dependencies) is
   merged field by field with the same rules.
4. If both sides moved a package to different versions, keep the higher one.
5. Restore packages that a remaining dependency edge still needs (one branch
   removed `requests` and with it `certifi`, the other added `httpx`, which
   needs `certifi`). Drop packages that are no longer reachable from the
   workspace.
6. Write the result in uv's layout. The writer follows uv's own lock writer
   (`crates/uv-lock/src/lock/serialize.rs`): which arrays are one element per
   line, which depend on their length, when table headers appear, how strings
   are quoted. Parsing and writing 60 public `uv.lock` files (6216 package
   entries, revisions 1 to 5, including workspaces, forks, git, path and URL
   sources, conflicts and exclude-newer settings) reproduced every file byte
   for byte.

If something cannot be merged without guessing, the driver leaves a normal
git conflict (`git merge-file` markers) and prints why. That happens when both
sides change `requires-python`, the resolution markers or conflict sets
differently, when both sides change the versions of a package that already has
several versions (a forked resolution), when the same package version differs
in content on both sides, or when the lockfile is not version 1.

## Tests

```sh
python -m unittest tests.test_unit -v   # offline: writer, merge rules, driver fallback, install
python -m unittest tests.test_e2e -v    # needs git, uv and network access to PyPI
```

The end to end tests build a repository, create two branches that change
dependencies with real `uv lock` runs, merge them through git with the driver
installed and finish with `uv lock --locked`. Scenarios: both branches add a
dependency; upgrades on both branches (and the two one-side workarounds that
lose them); a removed dependency next to an added one; both branches upgrade
the same package; workspace members; a forked resolution; a `requires-python`
conflict that must fall back to markers; `resolve` during a rebase. CI runs
them with uv latest, 0.11.1 and 0.8.0, and the offline tests on Python 3.11 to
3.14.

## Limits

- The merged lockfile is a structural merge, not a new resolution. uv checks
  it on the next `uv lock`, `uv sync` or `uv run`, and re-resolves if the
  merged `pyproject.toml` needs something else. Run `uv lock --locked` in CI to
  catch a merge that needs a re-lock.
- Two independent upgrades can be incompatible with each other. The driver
  does not detect that; uv does when it next resolves.
- "Keep the higher version" is a choice, not a resolution. It is printed so
  that it can be reviewed.
- Only lockfile version 1 (the current default) is supported. uv's
  experimental version 2 falls back to a normal conflict.
- Merging the 3.2 MB lockfile of Apache Airflow takes about 5 seconds, most of
  it in `tomllib`; lockfiles of a few hundred KB take well under a second.
- Script lockfiles (`script.py.lock`) are not registered by `install`; add an
  attribute line for them by hand if you use them.

## License

MIT
