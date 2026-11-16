"""Planungszahlen (WM-151): the planning insights of `metrics.planning.insights`, team only.

`GET /analyse/planung` (Team route, `access.py`) shows them for the time controls of the analysis
(period and time bin; default this school year). It names team members (hours per person) and
courses, so it is never public; the URL carries only the filters. Changing a filter reloads the
page (static/app.js, `data-reload`); `GET /api/analyse/planung/stunden` is the chart of the team's
hours, where a click on a bar chooses that period.
"""

from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from metrics import analytics, charts
from metrics.db import get_sessionmaker
from metrics.instance import get_instance
from metrics.planning import insights, workflow, workload
from metrics.web.common import page, page_context, parse_filters, today

router = APIRouter()

DEFAULT_PERIOD = "this_school_year"
MAX_COLUMNS = 24  # more time bins than this: the table of hours per person is left out


def _filters(request: Request):
    params = request.query_params
    filters = parse_filters(params)
    if "period" not in params:
        filters.preset = DEFAULT_PERIOD
    return filters


@router.get("/analyse/planung", response_class=HTMLResponse)
def planning_insights(request: Request):
    filters = _filters(request)
    instance = get_instance()
    year_start = instance.school_year_start_month
    now = today()
    with get_sessionmaker()() as session:
        hours = insights.team_hours(session, filters, now, year_start)
        context = page_context(
            filters,
            hours=hours,
            max_columns=MAX_COLUMNS,
            equipment=insights.equipment_usage(session, filters, now, year_start),
            cancellations=insights.cancellations(session, filters, now, year_start),
            lead_time=insights.lead_time(session, filters, now, year_start),
            unbilled=[(t, workflow.title(t)) for t in insights.unbilled(session, filters, now, year_start)],
            number=workload.number,
            formula=workload.formula(),
            default_period=DEFAULT_PERIOD,
        )
        return page(request, "planungszahlen.html", context)


@router.get("/api/analyse/planung/stunden")
def hours_chart(request: Request) -> JSONResponse:
    """The team's estimated hours per time bin, lead and support stacked."""
    filters = _filters(request)
    instance = get_instance()
    with get_sessionmaker()() as session:
        hours = insights.team_hours(session, filters, today(), instance.school_year_start_month)
    bins = [
        {
            "start": b.start.isoformat(),
            "end": b.end.isoformat(),
            "label": b.label,
            "lead": total.lead,
            "support": total.support,
        }
        for b, total in zip(hours.bins, hours.totals, strict=True)
    ]
    return JSONResponse(
        {
            "spec": charts.hours_over_time(bins, filters.bin, instance.theme),
            "meta": {
                "count": sum(t.termine for t in hours.totals),
                "hours": float(sum((t.total for t in hours.totals), 0)),
                "period": analytics.period_caption(
                    hours.bins[0].start, hours.bins[-1].end - timedelta(days=1)
                )
                if hours.bins
                else "",
            },
        }
    )
