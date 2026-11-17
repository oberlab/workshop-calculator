"""Datenschutz and Impressum (WM-153): the public pages and the team part of the privacy notice,
placeholders from the instance config, and the privacy notice's numbers read from the code's own
constants."""

import datetime as dt
import re

import pytest

from metrics import auth, backup, instance, kimai, logins, oidc
from metrics.web import access, common, login, public, security
from metrics.web import privacy as page
from tests.conftest import EXAMPLE_INSTANCE
from tests.test_web import _client

PRIVACY = '''
[privacy]
responsible = """
Testverein e. V.
Teststraße 7
99999 Prüfstadt
"""
contact_email = "datenschutz@test.invalid"
hosting = "Testhoster in Testland"
legal_basis = "Testgrundlage für alle"
supervisory_authority = """
Testaufsicht
Amtsweg 1
"""
dpo = "Frau Test, dsb@test.invalid"
updated = 2026-12-01
'''

IMPRINT = '''
[imprint]
name = "Testverein e. V."
address = """
Teststraße 7
99999 Prüfstadt
"""
represented_by = "Vorstand A und B"
register = "Amtsgericht Prüfstadt, VR 123"
email = "kontakt@test.invalid"
phone = "+49 1 23"
vat_id = "DE123"
responsible_content = "Person C"
extra = """
Erster Absatz.

Zweiter Absatz.
"""
'''


def _with_config(monkeypatch, tmp_path, text: str) -> None:
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    instance.get_instance.cache_clear()


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _as_admin(monkeypatch, admin=True):
    """A login without a database: the login check, the access rule and the sidebar counters stubbed."""
    person = auth.Login("Admin" if admin else "Team", admin=admin, subject="a")
    monkeypatch.setattr(common, "current_login", lambda request: person)
    monkeypatch.setattr(access, "current_login", lambda request: person)
    monkeypatch.setattr(common, "list_state", lambda login=None: common.ListState([], {}))


def _team_page(monkeypatch) -> str:
    client = _client(monkeypatch)
    _as_admin(monkeypatch, admin=False)
    return client.get("/datenschutz/team").text


# Facts that belong to the team part only: they never appear on the public page.
TEAM_ONLY = ("Slack", "Kimai", "Kontaktperson", "Wochenstunden", "Abwesenheit", "Sicherung", "Verlauf")


@pytest.mark.parametrize("path", ["/datenschutz", "/impressum"])
def test_pages_are_public(monkeypatch, path):
    response = _client(monkeypatch).get(path, follow_redirects=False)
    assert response.status_code == 200
    assert "<script>" not in response.text  # no inline script (the CSP would block it anyway)


@pytest.mark.parametrize("path", ["/", "/login", "/datenschutz", "/impressum"])
def test_every_page_links_both(monkeypatch, path):
    html = _client(monkeypatch).get(path).text
    footer = html[html.index('class="page-foot"') :]
    assert 'href="/datenschutz"' in footer and 'href="/impressum"' in footer
    assert 'href="/datenschutz/team"' not in footer  # only for logged-in team members


@pytest.mark.parametrize("path", ["/datenschutz", "/impressum", "/datenschutz/team"])
def test_logged_in_footer_links_the_team_part(monkeypatch, path):
    client = _client(monkeypatch)
    _as_admin(monkeypatch, admin=False)
    html = client.get(path).text
    footer = html[html.index('class="page-foot"') :]
    for href in ("/datenschutz", "/impressum", "/datenschutz/team"):
        assert f'href="{href}"' in footer
    assert ">Datenschutz (Team)</a>" in footer


def test_team_part_needs_login(monkeypatch):
    client = _client(monkeypatch)
    response = client.get("/datenschutz/team", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert access.level("GET /datenschutz/team") == access.TEAM


def test_public_part_has_no_team_facts(monkeypatch, tmp_path):
    _with_config(monkeypatch, tmp_path, PRIVACY)
    html = _client(monkeypatch).get("/datenschutz").text
    main = _text(html[html.index('id="main"') : html.index('class="page-foot"')])
    for word in TEAM_ONLY:
        assert word not in main, word
    assert "Art. 15" in main and "Art. 77" in main and "Testaufsicht" in main
    assert "zusammengefasste Kennzahlen" in main and "keine personenbezogenen Daten" in main
    link = html[html.index("data-team-link") :].split("</p>")[0]
    assert 'href="/datenschutz/team"' in link


def test_team_part_shows_the_team_facts(monkeypatch, tmp_path):
    _with_config(monkeypatch, tmp_path, PRIVACY)
    html = _team_page(monkeypatch)
    main = _text(html[html.index('id="main"') : html.index('class="page-foot"')])
    for word in TEAM_ONLY:
        assert word in main, word
    for section in ("zweck", "daten", "zugriff", "dienste", "speicherdauer", "rechte"):
        assert f'id="{section}"' in html
    assert "Art. 15" in main and "Art. 77" in main and 'href="/datenschutz#rechte"' in html
    assert 'href="/datenschutz"' in html[html.index("data-public-link") :].split("</p>")[0]
    assert "Testhoster in Testland" in html and 'href="mailto:datenschutz@test.invalid"' in html
    assert "Stand: 01.12.2026" in html and "data-draft" not in html
    assert "<script>" not in html


def test_team_part_placeholders_and_admin_notice(monkeypatch, tmp_path):
    _with_config(monkeypatch, tmp_path, "")
    client = _client(monkeypatch)
    _as_admin(monkeypatch)
    html = client.get("/datenschutz/team").text
    for key in ("contact_email", "hosting", "legal_basis"):
        assert f'data-placeholder="{key}"' in html
    assert "data-admin-notice" in html and "data-draft" in html and "Entwurf, noch nicht freigegeben" in html


def test_legal_basis_for_visitors(monkeypatch, tmp_path):
    """Without `legal_basis_visitors` both parts show `legal_basis`; with it the public part has its own."""
    _with_config(monkeypatch, tmp_path, PRIVACY)
    public_html = _client(monkeypatch).get("/datenschutz").text
    assert "Testgrundlage für alle" in public_html.split("data-legal-basis")[1].split("</p>")[0]
    _with_config(monkeypatch, tmp_path, PRIVACY + 'legal_basis_visitors = "Nur für Besuch"\n')
    public_html = _client(monkeypatch).get("/datenschutz").text
    basis = public_html.split("data-legal-basis")[1].split("</p>")[0]
    assert "Nur für Besuch" in basis and "Testgrundlage" not in public_html
    team_basis = _team_page(monkeypatch).split("data-legal-basis")[1].split("</p>")[0]
    assert "Testgrundlage für alle" in team_basis and "Nur für Besuch" not in team_basis


def test_login_page_links_both_in_its_text(monkeypatch):
    html = _client(monkeypatch).get("/login").text
    text = html[html.index('class="help login-legal"') :]
    assert 'href="/datenschutz"' in text.split("</p>")[0] and 'href="/impressum"' in text.split("</p>")[0]


def test_privacy_placeholders_when_section_missing(monkeypatch, tmp_path):
    _with_config(monkeypatch, tmp_path, "")
    html = _client(monkeypatch).get("/datenschutz").text
    for key in ("responsible", "contact_email", "hosting", "legal_basis", "supervisory_authority"):
        assert f'data-placeholder="{key}"' in html
    assert "[Verantwortliche Stelle (Name und Anschrift) in der Instanz-Konfiguration eintragen]" in html
    assert "data-draft" in html and "Entwurf, noch nicht freigegeben" in html
    assert "data-admin-notice" not in html  # a visitor never sees the notice for admins
    assert "Datenschutzbeauftragte" not in html  # optional: no line without a value


def test_example_instance_is_a_draft_with_placeholders():
    settings = instance.load_instance(EXAMPLE_INSTANCE)
    assert settings.privacy.draft and settings.privacy.missing == list(instance.Privacy.REQUIRED)
    assert settings.imprint.missing == list(instance.Imprint.REQUIRED)


def test_admins_see_which_keys_are_missing(monkeypatch, tmp_path):
    _with_config(monkeypatch, tmp_path, "[privacy]\ncontact_email = 'a@test.invalid'\n")
    client = _client(monkeypatch)
    _as_admin(monkeypatch)
    html = client.get("/datenschutz").text
    notice = html[html.index("data-admin-notice") :].split("</div>")[0]
    assert "<code>[privacy]</code>" in notice and "<code>responsible</code>" in notice
    assert "contact_email" not in notice
    html = client.get("/impressum").text
    notice = html[html.index("data-admin-notice") :].split("</div>")[0]
    assert "<code>[imprint]</code>" in notice and "<code>register</code>" in notice


def test_privacy_values_when_set(monkeypatch, tmp_path):
    _with_config(monkeypatch, tmp_path, PRIVACY)
    client = _client(monkeypatch)
    _as_admin(monkeypatch)
    html = client.get("/datenschutz").text
    assert "Testverein e. V.<br>Teststraße 7<br>99999 Prüfstadt" in html
    assert 'href="mailto:datenschutz@test.invalid"' in html
    values = ("Testhoster in Testland", "Testgrundlage für alle", "Testaufsicht<br>Amtsweg 1", "Frau Test")
    for value in values:
        assert value in html
    assert "data-placeholder" not in html and "data-admin-notice" not in html
    assert "data-draft" not in html and "Stand: 01.12.2026" in html


def test_config_values_are_escaped(monkeypatch, tmp_path):
    _with_config(monkeypatch, tmp_path, '[privacy]\nhosting = "<b>fett</b>"\n')
    html = _client(monkeypatch).get("/datenschutz").text
    assert "<b>fett</b>" not in html and "&lt;b&gt;fett&lt;/b&gt;" in html


def test_imprint_placeholders_when_section_missing(monkeypatch, tmp_path):
    _with_config(monkeypatch, tmp_path, "")
    html = _client(monkeypatch).get("/impressum").text
    assert "Angaben gemäß § 5 DDG" in html
    for key in ("name", "address", "represented_by", "register", "email"):
        assert f'data-placeholder="{key}"' in html
    for optional in ("Telefon", "Umsatzsteuer", "Verantwortlich für den Inhalt"):
        assert optional not in html


def test_imprint_values_when_set(monkeypatch, tmp_path):
    _with_config(monkeypatch, tmp_path, IMPRINT)
    html = _client(monkeypatch).get("/impressum").text
    assert "data-placeholder" not in html
    assert "Testverein e. V.<br>Teststraße 7<br>99999 Prüfstadt" in html
    values = ("Vorstand A und B", "Amtsgericht Prüfstadt, VR 123", "mailto:kontakt@test.invalid", "+49 1 23")
    for value in values:
        assert value in html
    assert "DE123" in html and "Person C" in html
    assert "<p>Erster Absatz.</p><p>Zweiter Absatz.</p>" in html


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[privacy]\nupdated = 'bald'\n", "updated"),
        ("[privacy]\nunknown = 'x'\n", "unknown"),
        ("[imprint]\nemail = 3\n", "must be text"),
    ],
)
def test_bad_config_is_refused(text, error, tmp_path):
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        instance.load_instance(tmp_path)


# --- The numbers come from the code ---------------------------------------------------------


def test_pages_state_the_current_constants(monkeypatch):
    text = _text(_client(monkeypatch).get("/datenschutz").text)
    for name in (auth.COOKIE_NAME, security.CSRF_COOKIE, oidc.STATE_COOKIE):
        assert name in text
    text = _text(_team_page(monkeypatch))
    assert "spätestens nach 12 Stunden und nach 2 Stunden ohne Aktivität" in text
    assert "30 Tage nach ihrem Ablauf gelöscht" in text
    assert "die neuesten 14 täglichen und 12 monatlichen Sicherungen" in text
    assert "bis zu 12 Monate" in text
    assert "der vergangenen 31 Tage und der kommenden 400 Tage" in text


def test_pruning_uses_the_constants_the_page_shows():
    """The page reads the constants; the pruning must use the same ones, not literals of its own."""
    assert backup.prune.__defaults__ == (backup.KEEP_DAILY, backup.KEEP_MONTHLY)


@pytest.mark.parametrize(
    ("module", "name", "value", "fact", "expected"),
    [
        (logins, "LIFETIME", dt.timedelta(hours=7), "sessions", "spätestens nach 7 Stunden"),
        (logins, "LIFETIME", dt.timedelta(hours=7), "session-cookie", "7 Stunden"),
        (logins, "IDLE", dt.timedelta(minutes=45), "sessions", "nach 45 Minuten ohne Aktivität"),
        (logins, "RETENTION", dt.timedelta(days=9), "sessions", "9 Tage nach ihrem Ablauf"),
        (backup, "KEEP_DAILY", 5, "backups", "die neuesten 5 täglichen"),
        (backup, "KEEP_MONTHLY", 3, "backups", "und 3 monatlichen"),
        (backup, "KEEP_MONTHLY", 3, "backups", "bis zu 3 Monate"),
        (oidc, "STATE_SECONDS", 300, "state-cookie", "5 Minuten"),
        (kimai, "PAST", dt.timedelta(days=8), "absences", None),
    ],
)
def test_page_follows_a_changed_constant(monkeypatch, module, name, value, fact, expected):
    """Cookies are in the public part, retention and services in the team part."""
    monkeypatch.setattr(module, name, value)
    if fact.endswith("-cookie"):
        html = _client(monkeypatch).get("/datenschutz").text
    else:
        html = _team_page(monkeypatch)
    if expected is None:  # Kimai's window is stated in the services section
        assert "der vergangenen 8 Tage" in _text(html)
        return
    cell = html[html.index(f'data-fact="{fact}"') :].split("</td>")[0]
    assert expected in _text(cell)


def test_rate_limit_windows_follow_the_code(monkeypatch):
    monkeypatch.setattr(login.LOGIN_LIMIT, "window", 120)
    monkeypatch.setattr(public.DATA_LIMIT, "window", 30)
    text = _text(_client(monkeypatch).get("/datenschutz").text)
    assert "Anmeldung: Zeitfenster von 2 Minuten" in text
    assert "Daten der Übersicht: Zeitfenster von 30 Sekunden" in text


def test_job_times_come_from_the_scheduler():
    facts = page.facts()
    assert facts["backup_when"] == "täglich um 03:30 Uhr"
    assert facts["kimai_when"] == "stündlich"
    assert facts["calendar_when"] == "alle 15 Minuten"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (dt.timedelta(days=1), "1 Tag"),
        (dt.timedelta(hours=36), "36 Stunden"),
        (dt.timedelta(seconds=90), "90 Sekunden"),
        (dt.timedelta(minutes=1), "1 Minute"),
    ],
)
def test_duration(value, text):
    assert page.duration(value) == text
