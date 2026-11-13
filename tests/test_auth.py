from metrics import auth

SECRET = b"test-secret"


def test_password_roundtrip():
    stored = auth.hash_password("korrekt-pferd-batterie")
    assert "$" not in stored  # safe in a Compose .env file
    assert auth.verify_password("korrekt-pferd-batterie", stored)
    assert not auth.verify_password("falsch", stored)


def test_verify_rejects_missing_or_malformed_hash():
    assert not auth.verify_password("x", None)
    assert not auth.verify_password("x", "")
    assert not auth.verify_password("x", "bcrypt:1:2:3:00:00")
    assert not auth.verify_password("x", "scrypt:kaputt")


def test_signed_value_until_expiry():
    state = auth.sign({"state": "x"}, 600, now=1000, secret=SECRET)
    assert auth.unsign(state, now=1001, secret=SECRET) == {"state": "x", "exp": 1600}
    assert auth.unsign(state, now=1601, secret=SECRET) is None


def test_signed_value_rejects_tampering_and_other_secret():
    state = auth.sign({"state": "x"}, 600, now=1000, secret=SECRET)
    forged = auth.sign({"state": "y"}, 600, now=1000, secret=b"other")
    assert (
        auth.unsign(f"{forged.rpartition('.')[0]}.{state.rpartition('.')[2]}", now=1001, secret=SECRET)
        is None
    )
    assert auth.unsign(state, now=1001, secret=b"other") is None
    assert auth.unsign(None) is None
    assert auth.unsign("unsinn", secret=SECRET) is None


def test_password_login_needs_explicit_switch(monkeypatch):
    from metrics import config

    monkeypatch.setenv("AUTH_MODE", "password")
    assert not config.get_settings().allow_password_login
    assert not auth.password_login_allowed()
    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "true")
    config.get_settings.cache_clear()
    assert auth.password_login_allowed()
