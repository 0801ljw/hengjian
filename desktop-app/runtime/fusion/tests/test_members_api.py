import json
import urllib.error
import urllib.request

from fusion.tests.test_http_server import call, http  # noqa: F401 - fixture


def raw(base, method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_member_management_requires_admin(http):
    httpd, base = http
    httpd.users.add_user("root", "root-password-1", "admin")
    assert call(base, "/api/members")[0] == 401
    status, data = raw(base, "POST", "/api/login", {"username": "root", "password": "root-password-1"})
    assert status == 200
    token = data["session"]
    status, data = raw(base, "GET", "/api/members", token=token)
    assert status == 200 and [item["name"] for item in data["members"]] == ["root"]
    status, data = raw(base, "POST", "/api/members",
                       {"username": "ljw", "password": "ljw-password-1", "role": "operator"}, token=token)
    assert status == 201 and {item["name"] for item in data["members"]} == {"root", "ljw"}
    assert raw(base, "DELETE", "/api/members/root", token=token)[0] == 409
    assert raw(base, "DELETE", "/api/members/ljw", token=token)[0] == 200
