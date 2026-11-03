"""Three-way merge of parsed uv.lock files (lockfile version 1)."""

from __future__ import annotations

import json

from .pep440 import version_key


class Unresolved(Exception):
    """The two sides changed the same thing in ways this tool will not guess."""


_MISSING = object()

# Values that must change on one side only. A set-style merge of these could
# produce marker spaces that no resolution of the merged project has.
_ATOMIC_KEYS = {
    "requires-python",
    "resolution-markers",
    "supported-markers",
    "required-markers",
    "conflicts",
}

# Tables in a [[package]] entry whose values are lists of dependency edges.
_EDGE_TABLES = ("optional-dependencies", "dev-dependencies")


def _ident(value) -> str:
    return json.dumps(value, sort_keys=True)


def _sort_name(x):
    if isinstance(x, dict) and isinstance(x.get("name"), str):
        return x["name"]
    if isinstance(x, str):
        return x
    return None


def _is_sorted(items: list) -> bool:
    keys = [_sort_name(x) for x in items]
    return None not in keys and all(a <= b for a, b in zip(keys, keys[1:]))


def _insert_relative(result: list, ids: list, additions: list, theirs_ids: list, by_name: bool):
    """Insert each addition after its nearest predecessor in theirs.

    uv sorts most lists by package name; when both sides are sorted that way
    and the name is new, the addition goes to its sorted position instead.
    """
    for x in additions:
        xid = _ident(x)
        name = _sort_name(x)
        if by_name and name not in [_sort_name(r) for r in result]:
            at = sum(1 for r in result if _sort_name(r) < name)
        else:
            pos = theirs_ids.index(xid)
            at = 0
            for j in range(pos - 1, -1, -1):
                if theirs_ids[j] in ids:
                    at = ids.index(theirs_ids[j]) + 1
                    break
        result.insert(at, x)
        ids.insert(at, xid)


def _merge_list(base: list, ours: list, theirs: list) -> list:
    b = {_ident(x) for x in base}
    o_ids = [_ident(x) for x in ours]
    t_ids = [_ident(x) for x in theirs]
    o, t = set(o_ids), set(t_ids)
    removed = (b - o) | (b - t)
    result = [x for x, xid in zip(ours, o_ids) if xid not in removed]
    ids = [xid for xid in o_ids if xid not in removed]
    additions = [x for x, xid in zip(theirs, t_ids) if xid not in b and xid not in o]
    _insert_relative(result, ids, additions, t_ids, _is_sorted(ours) and _is_sorted(theirs))
    return result


def _merge_keys(ours: dict, theirs: dict) -> list:
    keys = list(ours)
    t_keys = list(theirs)
    for i, k in enumerate(t_keys):
        if k in keys:
            continue
        at = 0
        for j in range(i - 1, -1, -1):
            if t_keys[j] in keys:
                at = keys.index(t_keys[j]) + 1
                break
        keys.insert(at, k)
    return keys


def merge_value(path: str, base, ours, theirs):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    key = path.rsplit(".", 1)[-1]
    if path == "revision" and isinstance(ours, int) and isinstance(theirs, int):
        return max(ours, theirs)
    if key not in _ATOMIC_KEYS:
        if isinstance(ours, dict) and isinstance(theirs, dict):
            b = base if isinstance(base, dict) else {}
            merged = {}
            for k in _merge_keys(ours, theirs):
                v = merge_value(
                    f"{path}.{k}" if path else k,
                    b.get(k, _MISSING),
                    ours.get(k, _MISSING),
                    theirs.get(k, _MISSING),
                )
                if v is not _MISSING:
                    merged[k] = v
            return merged
        if isinstance(ours, list) and isinstance(theirs, list):
            return _merge_list(base if isinstance(base, list) else [], ours, theirs)
    raise Unresolved(f"both sides changed `{path}`")


def _pkg_key(pkg: dict) -> tuple:
    return (pkg.get("name"), pkg.get("version"), _ident(pkg.get("source")))


def _index(packages: list, side: str) -> dict:
    out = {}
    for pkg in packages:
        k = _pkg_key(pkg)
        if k in out:
            raise Unresolved(f"{side}: duplicate package entry {k[0]} {k[1]}")
        out[k] = pkg
    return out


def _edges(pkg: dict):
    """Yield (container list, index, edge) for every dependency edge of a package."""
    deps = pkg.get("dependencies")
    if isinstance(deps, list):
        for i, e in enumerate(deps):
            yield deps, i, e
    for table in _EDGE_TABLES:
        groups = pkg.get(table)
        if isinstance(groups, dict):
            for lst in groups.values():
                for i, e in enumerate(lst):
                    yield lst, i, e


def _targets(edge: dict, by_name: dict) -> list:
    entries = by_name.get(edge.get("name"), [])
    if "version" in edge:
        entries = [p for p in entries if p.get("version") == edge["version"]]
    if "source" in edge:
        entries = [p for p in entries if p.get("source") == edge["source"]]
    return entries


def _by_name(packages) -> dict:
    out: dict = {}
    for p in packages:
        out.setdefault(p.get("name"), []).append(p)
    return out


def _roots(lock: dict, packages: list) -> list:
    manifest = lock.get("manifest", {})
    names = set(manifest.get("members", []))
    for req in manifest.get("requirements", []):
        names.add(req.get("name"))
    for reqs in manifest.get("dependency-groups", {}).values():
        for req in reqs:
            names.add(req.get("name"))
    roots = []
    for p in packages:
        src = p.get("source", {})
        if p.get("name") in names or "editable" in src or "virtual" in src:
            roots.append(p)
    return roots


def _reachable(lock: dict, packages: list) -> set:
    by_name = _by_name(packages)
    seen: set = set()
    stack = list(_roots(lock, packages))
    while stack:
        p = stack.pop()
        k = _pkg_key(p)
        if k in seen:
            continue
        seen.add(k)
        for _, _, e in _edges(p):
            stack.extend(_targets(e, by_name))
    return seen


def _version_conflicts(result: dict, ours: dict, theirs: dict, notes: list) -> None:
    """Both sides moved one package to different versions: keep the higher one."""
    def count(d):
        c: dict = {}
        for k in d:
            c[k[0]] = c.get(k[0], 0) + 1
        return c

    rc, oc, tc = count(result), count(ours), count(theirs)
    for name, n in rc.items():
        limit = max(oc.get(name, 0), tc.get(name, 0))
        if n <= limit:
            continue
        if oc.get(name, 0) != 1 or tc.get(name, 0) != 1:
            raise Unresolved(f"both sides changed the forked versions of {name}")
        keys = [k for k in result if k[0] == name]
        keep = max(keys, key=lambda k: version_key(k[1] or ""))
        for k in keys:
            if k != keep:
                del result[k]
        dropped = ", ".join(k[1] for k in keys if k != keep)
        notes.append(f"{name}: both sides changed the version; kept {keep[1]} over {dropped}")


def merge_locks(base: dict, ours: dict, theirs: dict) -> tuple[dict, list[str]]:
    """Merge three parsed lockfiles. Returns (merged lock, notes)."""
    for side, lock in (("ours", ours), ("theirs", theirs)):
        if lock.get("version") != 1:
            raise Unresolved(f"{side}: unsupported lockfile version {lock.get('version')!r}")
    if base and base.get("version") != 1:
        raise Unresolved(f"base: unsupported lockfile version {base.get('version')!r}")

    notes: list[str] = []
    header = {}
    keys = _merge_keys(
        {k: v for k, v in ours.items() if k != "package"},
        {k: v for k, v in theirs.items() if k != "package"},
    )
    for k in keys:
        v = merge_value(k, base.get(k, _MISSING), ours.get(k, _MISSING), theirs.get(k, _MISSING))
        if v is not _MISSING:
            header[k] = v

    b = _index(base.get("package", []), "base")
    o = _index(ours.get("package", []), "ours")
    t = _index(theirs.get("package", []), "theirs")

    order = _merge_keys(o, t)
    result: dict = {}
    for k in order:
        bv, ov, tv = b.get(k), o.get(k), t.get(k)
        if ov == tv:
            v = ov
        elif ov == bv:
            v = tv
        elif tv == bv:
            v = ov
        elif ov is None or tv is None:
            v = ov if ov is not None else tv  # removed on one side, changed on the other
        else:
            v = merge_value("package", bv, ov, tv)
        if v is not None:
            result[k] = v

    _version_conflicts(result, o, t, notes)

    # Restore packages that a kept edge needs but the other side removed.
    for _ in range(len(o) + len(t) + 1):
        by_name = _by_name(result.values())
        missing = {
            e.get("name")
            for p in result.values()
            for _, _, e in _edges(p)
            if not by_name.get(e.get("name"))
        }
        if not missing:
            break
        for name in sorted(missing):
            for side in (o, t):
                entries = [k for k in side if k[0] == name]
                if entries:
                    for k in entries:
                        result[k] = side[k]
                    notes.append(f"{name}: kept, still required after the merge")
                    break
            else:
                raise Unresolved(f"{name} is required but missing on both sides")
    # uv sorts packages by name, then version; ties keep the side order.
    pos = {k: i for i, k in enumerate(order)}
    ordered = sorted(result, key=lambda k: (k[0], version_key(k[1] or ""), pos.get(k, len(pos))))

    packages = [result[k] for k in ordered]
    merged = dict(header)
    if packages or "package" in ours or "package" in theirs:
        merged["package"] = packages

    # Drop packages that only a removed edge reached.
    reach_sides = _reachable(ours, list(o.values())) | _reachable(theirs, list(t.values()))
    reach = _reachable(merged, packages)
    kept = []
    for p in packages:
        k = _pkg_key(p)
        if k in reach or k not in reach_sides:
            kept.append(p)
        else:
            notes.append(f"{k[0]} {k[1]}: dropped, no longer required")
    packages = kept
    if "package" in merged:
        merged["package"] = packages

    _fix_edge_identity(packages)
    return merged, notes


def _fix_edge_identity(packages: list) -> None:
    """uv writes version/source on an edge only when a name has several entries."""
    by_name = _by_name(packages)
    for p in packages:
        for lst, i, e in list(_edges(p)):
            n = len(by_name.get(e.get("name"), []))
            if n == 1 and ("version" in e or "source" in e):
                lst[i] = {k: v for k, v in e.items() if k not in ("version", "source")}
            elif n > 1 and "version" not in e:
                raise Unresolved(f"{p.get('name')}: edge to {e.get('name')} is ambiguous after the merge")
            elif n > 1 and not _targets(e, by_name):
                raise Unresolved(f"{p.get('name')}: edge to {e.get('name')} {e.get('version')} has no package")
