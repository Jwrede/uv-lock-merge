"""Order PEP 440 versions without third-party dependencies."""

from __future__ import annotations

import re

_VERSION = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?:-(?P<post_n1>[0-9]+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?)?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

_PRE_ORDER = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}
_INF = float("inf")


def version_key(text: str):
    """Sort key for a version string. Unparseable versions sort first."""
    m = _VERSION.match(text)
    if not m:
        return (-1, (), (), (), (), (), text)
    release = [int(x) for x in m.group("release").split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre_l, dev_l = m.group("pre_l"), m.group("dev_l")
    post = m.group("post_n1") or m.group("post_n2")
    has_post = m.group("post_n1") is not None or m.group("post_l") is not None
    if pre_l:
        pre = (_PRE_ORDER[pre_l.lower()], int(m.group("pre_n") or 0))
    elif dev_l and not has_post:
        pre = (-_INF, 0)  # 1.0.dev1 < 1.0a1
    else:
        pre = (_INF, 0)
    post_key = (int(post or 0),) if has_post else (-_INF,)
    dev_key = (int(m.group("dev_n") or 0),) if dev_l else (_INF,)
    local = m.group("local")
    local_key = (
        tuple((1, int(p), "") if p.isdigit() else (0, 0, p) for p in re.split(r"[-_.]", local.lower()))
        if local
        else ()
    )
    return (0, (int(m.group("epoch") or 0),), tuple(release), pre, post_key, dev_key, local_key)
