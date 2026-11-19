"""The instance theme: CSS variables and font files (the app's own files are mounted at /static)."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from metrics.instance import Theme, get_instance

router = APIRouter()


# Optional vertical metrics per font file, e.g. ascent_override = "114%": some fonts (Hind) leave more
# room below the letters than above, so text sits high in buttons and pills. Percentages only.
METRIC_OVERRIDES = ("ascent_override", "descent_override", "line_gap_override")
PERCENT = re.compile(r"\d{1,3}(\.\d+)?%")


def _metric_overrides(font: dict) -> str:
    parts = []
    for key in METRIC_OVERRIDES:
        value = str(font.get(key, "")).strip()
        if not value:
            continue
        if not PERCENT.fullmatch(value):
            raise ValueError(f'{key} of font {font.get("file")!r} must be a percentage like "114%"')
        parts.append(f"{key.replace('_', '-')}: {value}; ")
    return "".join(parts)


def theme_css_text(theme: Theme) -> str:
    faces = [
        f"@font-face {{ font-family: '{f['family']}'; src: url('/theme/fonts/{Path(f['file']).name}'); "
        f"font-weight: {f.get('weight', '100 900')}; font-style: normal; font-display: swap; "
        f"{_metric_overrides(f)}}}"
        for f in theme.font_files
    ]
    # A key like accent_ink becomes --color-accent-ink.
    lines = [f"  --color-{name.replace('_', '-')}: {value};" for name, value in theme.colors.items()]
    lines += [f"  --font-heading: {theme.heading_font};", f"  --font-body: {theme.body_font};"]
    return "\n".join([*faces, ":root {", *lines, "}", ""])


@router.get("/theme.css")
def theme_css() -> Response:
    return Response(theme_css_text(get_instance().theme), media_type="text/css")


FAVICON = Path(__file__).parent / "static" / "favicon.svg"


@router.get("/favicon.ico")
def favicon() -> FileResponse:
    """The app icon for clients that ask for /favicon.ico without reading the page's icon link."""
    return FileResponse(FAVICON, media_type="image/svg+xml")


@router.get("/theme/fonts/{name}")
def theme_font(name: str) -> FileResponse:
    theme = get_instance().theme
    files = {Path(f["file"]).name: theme.base_dir / f["file"] for f in theme.font_files}
    if name not in files or not files[name].is_file():
        raise HTTPException(404)
    return FileResponse(files[name])
