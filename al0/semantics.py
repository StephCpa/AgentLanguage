"""Execution compatibility is independent of SQLite's schema version."""
from .data import digest

FLOW_SEMANTICS = 2
EXECUTION_SEMANTICS = 2


def execution_site(node, path=()):
    """Every user name lives inside the same domain-separated structural key."""
    return 'site:' + digest(['site', list(path), node])
