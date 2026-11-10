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


def test_token_valid_until_expiry():
    token = auth.make_token("Alex", now=1000, secret=SECRET)
    assert auth.token_user(token, now=1001, secret=SECRET) == "Alex"
    assert auth.token_user(token, now=1000 + auth.SESSION_SECONDS + 1, secret=SECRET) is None


def test_token_rejects_tampering_and_other_secret():
    token = auth.make_token("Alex", now=1000, secret=SECRET)
    _, _, signature = token.rpartition(".")
    forged = auth.sign({"tier": "team", "user": "Mallory"}, 10**9, now=1000, secret=b"other")
    assert auth.token_user(f"{forged.rpartition('.')[0]}.{signature}", now=1001, secret=SECRET) is None
    assert auth.token_user(token, now=1001, secret=b"other") is None
    assert auth.token_user(None) is None
    assert auth.token_user("unsinn", secret=SECRET) is None


def test_signed_state_is_not_a_session():
    state = auth.sign({"state": "x"}, 600, now=1000, secret=SECRET)
    assert auth.unsign(state, now=1001, secret=SECRET) == {"state": "x", "exp": 1600}
    assert auth.token_user(state, now=1001, secret=SECRET) is None


def test_token_keeps_admin_flag():
    plain = auth.make_token("Alex", now=1000, secret=SECRET)
    admin = auth.make_token("Alex", now=1000, secret=SECRET, admin=True)
    assert auth.token_login(plain, now=1001, secret=SECRET) == auth.Login("Alex", admin=False)
    assert auth.token_login(admin, now=1001, secret=SECRET) == auth.Login("Alex", admin=True)
