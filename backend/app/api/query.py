"""Query API for scripts and LLM tools. External tools depend on these shapes."""

from fastapi import APIRouter

from app.api.deps import DbDep
from app.api.schemas import LLMQueryRequest
from app.repositories import logs as logs_repo
from app.repositories import tenants as tenants_repo
from app.services.query import MAX_LIMIT, summarize

router = APIRouter(prefix="/api/query", tags=["query"])


@router.get("/schema")
async def llm_query_schema(db: DbDep):
    """Describes the query API for LLM tool-use / function-calling."""
    tenants = await tenants_repo.get_tenants(db)

    return {
        "description": (
            "CPI Log Lens query API. Use POST /api/query to search SAP CPI log entries. "
            "Use GET /api/stats to get aggregated statistics."
        ),
        "endpoints": {
            "POST /api/query": {
                "description": "Search log entries with optional filters.",
                "body": {
                    "tenant": "string | null — tenant id to filter; null means all tenants",
                    "level": "string | null — log level: ERROR, WARN, INFO, DEBUG",
                    "iflow": "string | null — partial IFlow name (case-insensitive LIKE match)",
                    "grep": (
                        "string | null — case-insensitive text search in message and logger; "
                        "when no range is supplied, searches the last 24 hours"
                    ),
                    "date_from": (
                        "string | null — inclusive ISO date/datetime lower bound; timezone-less values "
                        "are interpreted as UTC and offset-aware values are normalized to UTC"
                    ),
                    "date_to": (
                        "string | null — inclusive ISO date/datetime upper bound; a bare date includes the full day"
                    ),
                    "limit": "integer 1–200 — max entries to return (default 50)",
                },
                "response": {
                    "total_matching": "total rows matching the filter (may exceed limit)",
                    "returned": "number of rows returned",
                    "summary": "short natural-language summary of the result",
                    "items": "array of log entries",
                    "item_fields": "id, tenant, log_type, filename, timestamp, level, logger, iflow, message, ip, node",
                },
            },
            "GET /api/stats": {
                "description": "Aggregated statistics (level distribution, top error iflows, timeline).",
                "params": {"tenant": "optional tenant id"},
            },
            "GET /api/tenants": {
                "description": "List configured tenants.",
            },
        },
        "available_tenants": [{"id": t["id"], "name": t["name"]} for t in tenants],
        "tip": "Start with GET /api/query/schema to understand the data, then POST /api/query with filters.",
    }


@router.post("")
async def llm_query(req: LLMQueryRequest, db: DbDep):
    """LLM-friendly log query endpoint. Returns matching entries plus a natural-language summary."""
    limit = max(1, min(req.limit, MAX_LIMIT))
    date_from, date_to = logs_repo.effective_date_bounds(grep=req.grep, date_from=req.date_from, date_to=req.date_to)
    result = await logs_repo.query_logs(
        db,
        tenant=req.tenant,
        level=req.level,
        iflow=req.iflow,
        grep=req.grep,
        date_from=date_from,
        date_to=date_to,
        page=1,
        page_size=limit,
    )
    total = result["total"]
    items = result["items"]
    summary = summarize(
        tenant=req.tenant,
        level=req.level,
        iflow=req.iflow,
        grep=req.grep,
        date_from=date_from,
        date_to=date_to,
        total=total,
        returned=len(items),
        limit=limit,
    )
    return {
        "total_matching": total,
        "returned": len(items),
        "summary": summary,
        "items": items,
    }
