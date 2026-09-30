"""Natural-language summary of a /api/query result for LLM tools."""

MAX_LIMIT = 200


def summarize(
    *,
    tenant: str | None,
    level: str | None,
    iflow: str | None,
    grep: str | None,
    date_from: str | None,
    date_to: str | None,
    total: int,
    returned: int,
    limit: int,
) -> str:
    parts = []
    if tenant:
        parts.append(f"tenant={tenant}")
    if level:
        parts.append(f"level={level.upper()}")
    if iflow:
        parts.append(f"iflow~'{iflow}'")
    if grep:
        parts.append(f"grep='{grep}'")
    if date_from or date_to:
        parts.append(f"range=[{date_from or '...'} → {date_to or '...'}]")

    filter_desc = ", ".join(parts) if parts else "no filters"
    summary = (
        f"Found {total} log entr{'y' if total == 1 else 'ies'} matching {filter_desc}. Returning {returned} of {total}."
    )
    if total > limit:
        summary += f" Use a stricter filter or increase limit (max {MAX_LIMIT}) to see more."
    return summary
