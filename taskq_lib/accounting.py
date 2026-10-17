"""What each claude call cost, and how full its context window was.

The CLI's `total_cost_usd` is the running total of its session, so a session that is
resumed (the coordinator's) reports the same money again on every turn. The queue
logs the increase instead (`cost_usd`) and keeps the last total per session.
"""


def cost_increase(costs, session_id, total, resumed_from=None, keep=500):
    """(what this call added, the updated {session_id: last total} table).

    A session seen for the first time adds its whole total. A session already in the table
    adds the rise over its last total, and a total that fell is a new run, so it counts whole.
    `resumed_from` is the session this call continued, for a CLI that gives a resumed session a
    new id but keeps the running total. The table keeps the `keep` newest sessions.
    """
    if not isinstance(total, (int, float)):
        return None, costs
    if not session_id:
        return total, costs
    previous = costs.get(session_id)
    if previous is None and resumed_from:
        previous = costs.get(resumed_from)
    costs = {k: v for k, v in costs.items() if k != session_id}
    costs[session_id] = total
    costs = dict(list(costs.items())[-keep:])
    if previous is None or total < previous:
        return total, costs
    return round(total - previous, 10), costs


def fill_costs(records):
    """The cost of each record, in file order, for rows written before `cost_usd` existed.

    Only the coordinator resumes a session; its rows are totals that grow, so the cost is the rise
    (and a total that drops starts over). A worker, review or probe is a fresh session, so its total
    is its cost. A row with `cost_usd` keeps it; one with no total costs nothing.
    """
    last = {}
    costs = []
    for record in records:
        if isinstance(record.get("cost_usd"), (int, float)):
            costs.append(record["cost_usd"])
            continue
        total = record.get("total_cost_usd")
        if not isinstance(total, (int, float)):
            costs.append(0.0)
            continue
        if record.get("label") == "coordinator":
            previous = last.get("coordinator")
            last["coordinator"] = total
            costs.append(total if previous is None or total < previous else total - previous)
        else:
            costs.append(total)
    return costs


def tokens_of(usage):
    """Tokens that are billed fresh: input, cache writes and output. Cache reads are left out, since they
    are most of a long session's volume and cost a tenth as much."""
    usage = usage if isinstance(usage, dict) else {}
    return sum(int(usage.get(key) or 0) for key in ("input_tokens", "cache_creation_input_tokens", "output_tokens"))


def _window(model_usage, model):
    """The context window of the model that was called (its entry in modelUsage), else of the dearest one."""
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    entry = model_usage.get(model)
    if entry is None and model:
        entry = next((v for k, v in model_usage.items() if k.startswith(model) or model.startswith(k)), None)
    if entry is None:
        entry = max(model_usage.values(), key=lambda v: (v or {}).get("costUSD") or 0)
    window = (entry or {}).get("contextWindow")
    return window if isinstance(window, (int, float)) and window > 0 else None


def context_of(envelope, model):
    """(tokens in the context at the end of the call, percent of the model's window), None where unknown.

    The size is the last iteration's input + cache read + cache creation tokens, which is the prompt the
    model saw on its final step, not the sum over the whole call.
    """
    iterations = ((envelope or {}).get("usage") or {}).get("iterations") or []
    if not iterations:
        return None, None
    last = iterations[-1] or {}
    tokens = sum(int(last.get(key) or 0)
                 for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    window = _window((envelope or {}).get("modelUsage"), model)
    return tokens, (round(100 * tokens / window, 1) if window else None)
