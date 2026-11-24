from fusion.redact import redact_jsonl


def test_redact_jsonl_removes_credentials():
    raw = '{"step":"x","token":"secret-value","api_key":"sk-abcdefgh12345678","text":"Bearer abcdefgh12345678"}\n'
    out = redact_jsonl(raw)
    assert "secret-value" not in out and "sk-abcdefgh" not in out
    assert "[REDACTED]" in out
