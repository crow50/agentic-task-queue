"""Keep the coordinator's memory folder from growing without limit."""


def trim_to_fit(text, allowed_bytes):
    """The newest whole lines of `text` that fit in `allowed_bytes`, so a rolling notes file drops its oldest lines first."""
    if allowed_bytes <= 0:
        return ""
    if len(text.encode()) <= allowed_bytes:
        return text
    kept, used = [], 0
    for line in reversed(text.splitlines(keepends=True)):
        size = len(line.encode())
        if used + size > allowed_bytes:
            break
        kept.append(line)
        used += size
    return "".join(reversed(kept))
