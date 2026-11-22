from fusion.tests.test_http_server import call, http  # noqa: F401 - fixture


def test_alerts_api_reads_with_token_and_ack_requires_human(http):
    httpd, base = http
    assert call(base, "/api/alerts")[0] == 401
    token = "alerts-" + "a" * 40
    httpd.api.add_token("alerts-reader", token)
    status, data = call(base, "/api/alerts", token)
    assert status == 200 and data["alerts"] == []
    assert call(base, "/api/alerts/1/ack", token, method="POST")[0] == 403
