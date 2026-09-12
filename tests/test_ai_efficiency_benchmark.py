from __future__ import annotations

import importlib.util
from pathlib import Path


def test_fixture_benchmark_reduces_requests_and_input_characters():
    path = Path(__file__).resolve().parents[1] / "scripts/benchmark_ai_efficiency.py"
    spec = importlib.util.spec_from_file_location("benchmark_ai_efficiency", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.benchmark()
    assert result["after_requests"] < result["before_requests"]
    assert result["after_input_chars"] < result["before_input_chars"] / 10
    assert result["verified_cache_hits"] == 5
    assert result["deterministic_extractions"] == 2
