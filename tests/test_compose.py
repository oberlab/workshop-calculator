"""compose.yaml passes every setting to the app one by one; a setting missing there never reaches it.

FRONT_URL (WM-154) was missing at first: set in the server's .env, it would have had no effect.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Settings the web app does not need: the scheduler writes the backups.
NOT_FOR_THE_APP = {"BACKUP_DIR"}


def _settings_read_by_the_code() -> set[str]:
    source = (ROOT / "src/metrics/config.py").read_text()
    return set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', source))


def _app_environment() -> set[str]:
    compose = (ROOT / "compose.yaml").read_text()
    app = re.search(r"\n  app:\n(.*?)\n  [a-z]+:\n", compose, re.S).group(1)
    environment = re.search(r"\n    environment:\n((?:      .*\n|\s*#.*\n)+)", app + "\n").group(1)
    return set(re.findall(r"^      ([A-Z_]+):", environment, re.M))


def test_compose_passes_every_setting_to_the_app():
    names = _settings_read_by_the_code()
    assert {"PUBLIC_URL", "FRONT_URL"} <= names
    assert names - NOT_FOR_THE_APP <= _app_environment()
