import json
import urllib.error
import urllib.request

from fusion.tests.test_http_server import call, http  # noqa: F401 - fixture


def post(base, path, token, payload):
    req = urllib.request.Request(base + path, method="POST",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_issue_queue_api(http):
    httpd, base = http
    assert call(base, "/api/issues")[0] == 401
    token = "issues-" + "a" * 40
    httpd.api.add_token("issue-worker", token)
    status, issue = post(base, "/api/issues", token, {
        "title": "container smoke", "assignee": "gongbu",
        "reviewer": "menxia", "approver": "human",
        "acceptance": ["file exists"],
    })
    assert status == 201 and issue["state"] == "queued"
    status, claimed = post(base, f"/api/issues/{issue['id']}/claim", token, {"worker": "agent-1"})
    assert status == 200 and claimed["state"] == "running"
    status, reviewing = post(base, f"/api/issues/{issue['id']}/complete", token, {
        "claim_token": claimed["claim_token"], "summary": "done", "artifacts": ["a.txt"],
    })
    assert status == 200 and reviewing["state"] == "review"
    assert post(base, f"/api/issues/{issue['id']}/approve", token, {})[0] == 403
    status, rows = call(base, "/api/issues", token)
    assert status == 200 and [row["id"] for row in rows["issues"]] == [issue["id"]]
