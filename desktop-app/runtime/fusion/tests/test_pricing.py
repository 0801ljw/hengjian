from fusion.pricing import estimate, load, save


def test_pricing_is_opt_in_and_deterministic(tmp_path):
    assert estimate(tmp_path, "m", 10, 5)["pricing"] == "unpriced"
    save(tmp_path, {"models": {"m": {"input_per_million_usd": 1,
                                     "output_per_million_usd": 2}}})
    assert load(tmp_path)["models"]["m"]["input_per_million_usd"] == 1
    result = estimate(tmp_path, "m", 10, 5)
    assert result["pricing"] == "configured"
    assert result["cost_usd"] == 0.00002
