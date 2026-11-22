"""First local registration creates the only admin; later signups are closed."""
from fusion.tests.test_auth import http, call


def test_first_registration_creates_admin_and_closes_signup(http):
    _, base = http
    status, payload = call(base, "/api/register", method="POST", body={
        "username": "founder", "password": "fixture-password-1"})
    assert status == 200 and payload["role"] == "admin" and payload["session"]
    session = payload["session"]
    assert call(base, "/api/me", session)[1]["name"] == "founder"
    status, payload = call(base, "/api/register", method="POST", body={
        "username": "second", "password": "fixture-password-2"})
    assert status == 403
    status, payload = call(base, "/api/login", method="POST", body={
        "username": "second", "password": "fixture-password-2"})
    assert status == 401
    status, payload = call(base, "/api/login", method="POST", body={
        "username": "founder", "password": "fixture-password-1"})
    assert status == 200 and payload["role"] == "admin"
