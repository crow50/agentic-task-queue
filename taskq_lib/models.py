"""The model table (models.json): which model does which job, what to fall back to, what a model can do.

    {"capabilities": {"<model>": "chat" | "code"},
     "needs": {"<role>": "chat" | "code"},
     "roles": {"<role>": {"<value_class>" | "default": ["<preferred>", "<fallback>", ...]}}}

A "code" model can use tools and so can do any job; a "chat" model can only converse, so it is never
chosen for a role that needs "code". Roles are worker, review and coordinator.
"""

import json

ROLES = ("worker", "review", "coordinator")
CAPABILITIES = ("chat", "code")

DEFAULT_TABLE = {
    "capabilities": {"claude-sonnet-5": "code", "claude-haiku-4-5-20251001": "code"},
    "needs": {"worker": "code", "review": "code", "coordinator": "code"},
    "roles": {
        "worker": {"default": ["claude-sonnet-5", "claude-haiku-4-5-20251001"]},
        "review": {"default": ["claude-haiku-4-5-20251001", "claude-sonnet-5"]},
        "coordinator": {"default": ["claude-sonnet-5", "claude-haiku-4-5-20251001"]},
    },
}


def problems(table):
    """What is wrong with a table, as sentences; an empty list when it is usable."""
    if not isinstance(table, dict):
        return ["the table must be a JSON object"]
    found = []
    capabilities = table.get("capabilities")
    if not isinstance(capabilities, dict) or not all(v in CAPABILITIES for v in capabilities.values()):
        found.append('"capabilities" must map each model to "chat" or "code"')
        capabilities = {}
    needs = table.get("needs", {})
    if not isinstance(needs, dict) or not all(k in ROLES and v in CAPABILITIES for k, v in needs.items()):
        found.append('"needs" must map a role (worker, review, coordinator) to "chat" or "code"')
    roles = table.get("roles")
    if not isinstance(roles, dict) or not roles:
        return found + ['"roles" must map each role to its chains']
    for role, by_class in roles.items():
        if role not in ROLES:
            found.append(f'unknown role "{role}"')
        elif not isinstance(by_class, dict) or not by_class:
            found.append(f'role "{role}" needs at least a "default" chain')
        else:
            for value_class, chain in by_class.items():
                if not isinstance(chain, list) or not chain or not all(isinstance(m, str) and m for m in chain):
                    found.append(f'{role}.{value_class} must be a non-empty list of model names')
                else:
                    found += [f'{role}.{value_class}: "{m}" has no entry in "capabilities"' for m in chain if m not in capabilities]
    return found


def load(path):
    """The table in `path`, or the built-in one when the file is missing. Raises ValueError when it is there but wrong."""
    try:
        text = path.read_text()
    except OSError:
        return DEFAULT_TABLE
    try:
        table = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from None
    if found := problems(table):
        raise ValueError(f"{path.name}: " + "; ".join(found))
    return table


def needs(table, role):
    return (table.get("needs") or {}).get(role, "code")


def can_do(table, model, role):
    """False only for a model the table knows to be too weak for the role; an unlisted model gets the benefit of the doubt."""
    kind = (table.get("capabilities") or {}).get(model)
    return kind is None or kind == "code" or needs(table, role) == "chat"


def chain(table, role, value_class=None):
    """The models for a role, preferred first, leaving out any that cannot do the job."""
    by_class = (table.get("roles") or {}).get(role) or {}
    models = by_class.get(value_class) or by_class.get("default") or []
    return [m for m in models if can_do(table, m, role)]


def resolve(table, role, value_class, wanted, unusable=()):
    """The model to call: `wanted` when it is usable, else the next one down the role's chain, else None.

    `wanted` may be "auto" for the chain's first usable model. A model the table knows cannot do the job counts as unusable.
    """
    unusable = set(unusable)
    options = chain(table, role, value_class)
    if wanted and wanted != "auto" and wanted not in unusable and can_do(table, wanted, role):
        return wanted
    after = options[options.index(wanted) + 1:] if wanted in options else options
    return next((m for m in after + options if m not in unusable), None)
