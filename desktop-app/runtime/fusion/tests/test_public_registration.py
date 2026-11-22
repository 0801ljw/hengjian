from fusion.registration import reset
from fusion.tests.test_auth import call, http  # noqa: F401 - fixture


def test_public_registration_creates_operator_after_admin(http, monkeypatch):
    _, base = http
    monkeypatch.setenv("DUTANG_PUBLIC_REGISTRATION", "1")
    monkeypatch.setenv("DUTANG_REGISTRATION_ROLE", "admin")
    reset()
    status, founder = call(base, "/api/register", method="POST", body={
        "username": "founder", "password": "fixture-password-1"})
    assert status == 200 and founder["role"] == "admin"
    status, member = call(base, "/api/register", method="POST", body={
        "username": "member", "password": "fixture-password-2"})
    assert status == 200 and member["role"] == "operator"
    status, login = call(base, "/api/login", method="POST", body={
        "username": "member", "password": "fixture-password-2"})
    assert status == 200 and login["role"] == "operator"
