"""The instance theme: CSS variables and font files (the app's own files are mounted at /static)."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from metrics.instance import Theme, get_instance

router = APIRouter()


def theme_css_text(theme: Theme) -> str:
    faces = [
        f"@font-face {{ font-family: '{f['family']}'; src: url('/theme/fonts/{Path(f['file']).name}'); "
        f"font-weight: {f.get('weight', '100 900')}; font-style: normal; font-display: swap; }}"
        for f in theme.font_files
    ]
    # A key like accent_ink becomes --color-accent-ink.
    lines = [f"  --color-{name.replace('_', '-')}: {value};" for name, value in theme.colors.items()]
    lines += [f"  --font-heading: {theme.heading_font};", f"  --font-body: {theme.body_font};"]
    return "\n".join([*faces, ":root {", *lines, "}", ""])


@router.get("/theme.css")
def theme_css() -> Response:
    return Response(theme_css_text(get_instance().theme), media_type="text/css")


@router.get("/theme/fonts/{name}")
def theme_font(name: str) -> FileResponse:
    theme = get_instance().theme
    files = {Path(f["file"]).name: theme.base_dir / f["file"] for f in theme.font_files}
    if name not in files or not files[name].is_file():
        raise HTTPException(404)
    return FileResponse(files[name])
