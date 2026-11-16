"""The "Tabelle" (WM-152): the held Termine of the analysis filters with chosen columns, sums per group
and the CSV download. Team only (`metrics.web.access`); the rows and the CSV come from `metrics.table`.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from metrics import analytics, metrics, table
from metrics.db import get_sessionmaker
from metrics.instance import get_instance
from metrics.web.analyse import filter_choices, period_label
from metrics.web.common import page, parse_filters, today

router = APIRouter()

# More rows than this are only in the CSV; the page says so.
SHOWN_ROWS = 2000

# Presets with a fixed year: their label names it, e.g. "Letztes Schuljahr (2025/26)".
NAMED_PRESETS = ("this_school_year", "last_school_year", "this_year", "last_year")


def _preset_labels(now, start_month: int) -> dict[str, str]:
    labels = {}
    for key, label in analytics.PRESETS.items():
        if key in NAMED_PRESETS:
            start, end = metrics.resolve_period(key, now, start_month)
            label = f"{label} ({period_label(key, start, end).removeprefix('Schuljahr ')})"
        labels[key] = label
    return labels


def period_text(preset: str, start, end) -> str:
    """The period of the selection in words, with its dates."""
    if start and end:
        dates = f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"
        return f"{period_label(preset, start, end)} ({dates})" if preset in NAMED_PRESETS else dates
    if start:
        return f"ab {start:%d.%m.%Y}"
    if end:
        return f"bis {end:%d.%m.%Y}"
    return "Gesamter Zeitraum"


def _load(request: Request):
    filters = parse_filters(request.query_params)
    choice = table.parse_choice(request.query_params)
    start_month = get_instance().school_year_start_month
    with get_sessionmaker()() as session:
        found, period = table.rows(session, filters, choice, today(), start_month)
        options = filter_choices(session)
    return filters, choice, found, period, options, start_month


@router.get("/tabelle", response_class=HTMLResponse)
def table_page(request: Request):
    filters, choice, found, period, options, start_month = _load(request)
    summary = table.groups(found, choice.group, start_month) if choice.group else []
    query = request.url.query
    return page(
        request,
        "tabelle.html",
        {
            "filters": filters,
            "choice": choice,
            "options": options,
            "presets": _preset_labels(today(), start_month),
            "columns": table.COLUMNS,
            "column_labels": {c.key: c.label for c in table.COLUMNS},
            "chosen": [table.BY_KEY[key] for key in choice.columns],
            "groups": table.GROUPS,
            "rows": found[:SHOWN_ROWS],
            "count": len(found),
            "shown": SHOWN_ROWS,
            "summary": summary,
            "total": table.total(found),
            "period": period_text(filters.preset, *period),
            "csv": "/tabelle.csv" + (f"?{query}" if query else ""),
            "csv_groups": f"/tabelle.csv?{query}&ansicht=summen" if choice.group else None,
            "text": table.text,
        },
    )


@router.get("/tabelle.csv")
def table_csv(request: Request) -> Response:
    """The table as CSV for Excel (German): the rows with the chosen columns, or with `ansicht=summen`
    the sums per group."""
    filters, choice, found, _, _, start_month = _load(request)
    if request.query_params.get("ansicht") == "summen" and choice.group:
        body = table.groups_csv(
            table.groups(found, choice.group, start_month), choice.group, table.total(found)
        )
        name = f"einsaetze-summen-{choice.group}.csv"
    else:
        body = table.rows_csv(found, choice.columns)
        name = "einsaetze.csv"
    return Response(
        body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )
