"""Write a parsed uv.lock (version 1) back in the layout uv itself produces.

The rules mirror uv's lock writer (crates/uv-lock/src/lock/serialize.rs):
which arrays are always multi-line, which depend on their length, which
stay inline, and when a table header is written.
"""

from __future__ import annotations

import re

# Arrays that uv always writes one element per line, keyed by table path
# ("" is the top level) and key. "*" matches any key in that table.
_ALWAYS_MULTILINE = {
    ("", "resolution-markers"),
    ("", "supported-markers"),
    ("", "required-markers"),
    ("manifest", "members"),
    ("package", "resolution-markers"),
    ("package", "dependencies"),
    ("package", "wheels"),
    ("package.optional-dependencies", "*"),
    ("package.dev-dependencies", "*"),
}

# Arrays written inline when they have at most one element, else multi-line.
_BY_LENGTH = {
    ("manifest", "requirements"),
    ("manifest", "constraints"),
    ("manifest", "overrides"),
    ("manifest", "excludes"),
    ("manifest", "build-constraints"),
    ("manifest.dependency-groups", "*"),
    ("package.metadata", "requires-dist"),
    ("package.metadata.requires-dev", "*"),
}

# Tables whose header uv omits when they only contain subtables.
_HEADER_ONLY_WITH_DIRECT_ENTRIES = {"manifest"}

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _matches(rules: set, path: str, key: str) -> bool:
    return (path, key) in rules or (path, "*") in rules


_PLAIN = re.compile(r'^[^"\\\x00-\x1f\x7f]*$')


def _string(value: str) -> str:
    if _PLAIN.match(value):
        return '"' + value + '"'
    # toml_edit's choice: a literal string only when it avoids escaping a
    # backslash and needs no other escapes; otherwise a basic string.
    if (
        "\\" in value
        and "'" not in value
        and not any(ord(c) <= 0x1F and c != "\t" or c == "\x7f" for c in value)
    ):
        return "'" + value + "'"
    out = []
    for c in value:
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif c == "\b":
            out.append("\\b")
        elif c == "\t":
            out.append("\\t")
        elif c == "\n":
            out.append("\\n")
        elif c == "\f":
            out.append("\\f")
        elif c == "\r":
            out.append("\\r")
        elif ord(c) <= 0x1F or c == "\x7f":
            out.append("\\u%04X" % ord(c))
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _string(key)


def inline(value) -> str:
    """Render a value on one line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return "[" + ", ".join(inline(v) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{_key(k)} = {inline(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"unsupported value in uv.lock: {value!r}")


def _multiline(values: list) -> str:
    return "[\n" + "".join(f"    {inline(v)},\n" for v in values) + "]"


def _array(path: str, key: str, values: list) -> str:
    if path == "" and key == "conflicts":
        return "[" + ", ".join(_multiline(group) for group in values) + "]"
    if _matches(_ALWAYS_MULTILINE, path, key):
        return "[]" if not values else _multiline(values)
    if _matches(_BY_LENGTH, path, key):
        return inline(values) if len(values) <= 1 else _multiline(values)
    return inline(values)


def _is_table(value) -> bool:
    return isinstance(value, dict)


def _is_table_array(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


_EXCLUDE_NEWER_COMMENT = " # This has no effect and is included for backwards compatibility when using relative exclude-newer values."


def detect_style(text: str) -> dict:
    """Layout details that differ between uv versions and cannot be parsed back."""
    return {
        # uv before 0.9.x wrote [manifest] even when it only had subtables.
        "manifest_header": "\n[manifest]\n" in text,
        # Newer uv annotates the placeholder exclude-newer timestamp.
        "exclude_newer_comment": _EXCLUDE_NEWER_COMMENT.strip() in text,
    }


def _write_table(out: list, path: list, table: dict, array_item: bool = False, style=None) -> None:
    """Write `table` (located at `path`) and its subtables."""
    # Paths of subtables inside [[package]] are written as package.<x>.
    rule_path = ".".join(path)
    direct = [(k, v) for k, v in table.items() if not _is_sub(rule_path, k, v)]
    subs = [(k, v) for k, v in table.items() if _is_sub(rule_path, k, v)]

    if path:
        header = ".".join(_key(p) for p in path)
        if array_item:
            out.append(f"\n[[{header}]]\n")
        elif (
            direct
            or rule_path not in _HEADER_ONLY_WITH_DIRECT_ENTRIES
            or (rule_path == "manifest" and style.get("manifest_header"))
        ):
            out.append(f"\n[{header}]\n")

    for key, value in direct:
        if isinstance(value, list):
            rendered = _array(rule_path, key, value)
        else:
            rendered = inline(value)
        if (
            key == "exclude-newer"
            and rule_path == "options"
            and "exclude-newer-span" in table
            and style.get("exclude_newer_comment", True)
        ):
            rendered += _EXCLUDE_NEWER_COMMENT
        out.append(f"{_key(key)} = {rendered}\n")

    for key, value in subs:
        if _is_table(value):
            _write_table(out, path + [key], value, style=style)
        else:
            for item in value:
                _write_table(out, path + [key], item, array_item=True, style=style)


# Keys holding arrays of tables, written as [[...]] sections.
_TABLE_ARRAYS = {("", "package"), ("manifest", "dependency-metadata")}
# Keys holding tables that uv writes as inline tables rather than sections.
_INLINE_TABLES = {"source", "sdist"}


def _is_sub(path: str, key: str, value) -> bool:
    if (path, key) in _TABLE_ARRAYS:
        return True
    if _is_table(value):
        if key in _INLINE_TABLES:
            return False
        # Inside options.exclude-newer-package, values are inline tables.
        if path == "options.exclude-newer-package":
            return False
        return True
    return False


def dumps(lock: dict, style: dict | None = None) -> str:
    """Serialize a parsed uv.lock dict. `style` comes from detect_style()."""
    out: list[str] = []
    _write_table(out, [], lock, style=style or {})
    return "".join(out)
