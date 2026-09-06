"""Prove agent-tunable settings actually read the env var names .env documents.

`DatabaseSettings` sets `env_prefix="MAXIMOR_"`, so without an explicit
`validation_alias`, a bare (unprefixed) key in `.env` is silently ignored and
the field silently falls back to its hardcoded default -- exactly the bug
found live: `.env`'s `DOCUMENT_ANALYSIS_TIMEOUT_SECONDS=150` never took
effect, and the real timeout was always the hardcoded 120s default. These
tests construct `DatabaseSettings` directly (never the `lru_cache`d
`get_database_settings()`) so each case is isolated.
"""

import pytest

from maximor.config import DatabaseSettings


def _settings(**overrides) -> DatabaseSettings:
    return DatabaseSettings(database_url="postgresql+asyncpg://test", **overrides)


def test_document_analysis_timeout_reads_bare_env_var(monkeypatch):
    """The unprefixed name .env actually uses must be effective."""

    monkeypatch.setenv("DOCUMENT_ANALYSIS_TIMEOUT_SECONDS", "150")
    assert _settings().document_analysis_timeout_seconds == 150


def test_document_analysis_timeout_reads_prefixed_env_var(monkeypatch):
    """The MAXIMOR_-prefixed name (the pydantic-settings default) must also work."""

    monkeypatch.setenv("MAXIMOR_DOCUMENT_ANALYSIS_TIMEOUT_SECONDS", "150")
    assert _settings().document_analysis_timeout_seconds == 150


@pytest.mark.parametrize(("field_name", "env_name", "value", "expected"), [
    ("document_analysis_max_turns", "DOCUMENT_ANALYSIS_MAX_TURNS", "7", 7),
    ("document_analysis_max_thinking_tokens", "DOCUMENT_ANALYSIS_MAX_THINKING_TOKENS", "1024", 1024),
    ("document_analysis_max_budget_usd", "DOCUMENT_ANALYSIS_MAX_BUDGET_USD", "5.5", 5.5),
    ("document_analysis_max_corrections", "DOCUMENT_ANALYSIS_MAX_CORRECTIONS", "0", 0),
    ("sku_mapping_max_turns", "SKU_MAPPING_MAX_TURNS", "9", 9),
    ("sku_mapping_max_thinking_tokens", "SKU_MAPPING_MAX_THINKING_TOKENS", "1536", 1536),
    ("sku_mapping_timeout_seconds", "SKU_MAPPING_TIMEOUT_SECONDS", "75", 75),
    ("sku_mapping_max_budget_usd", "SKU_MAPPING_MAX_BUDGET_USD", "2.5", 2.5),
    ("sku_mapping_max_corrections", "SKU_MAPPING_MAX_CORRECTIONS", "1", 1),
])
def test_agent_settings_consistency_pass_reads_bare_env_vars(monkeypatch, field_name, env_name, value, expected):
    """Every agent-tunable setting touched by the consistency pass honors its bare name."""

    monkeypatch.setenv(env_name, value)
    assert getattr(_settings(), field_name) == expected


@pytest.mark.parametrize(("field_name", "env_name", "value", "expected"), [
    ("document_analysis_max_turns", "MAXIMOR_DOCUMENT_ANALYSIS_MAX_TURNS", "7", 7),
    ("sku_mapping_timeout_seconds", "MAXIMOR_SKU_MAPPING_TIMEOUT_SECONDS", "75", 75),
])
def test_agent_settings_consistency_pass_still_reads_prefixed_env_vars(monkeypatch, field_name, env_name, value, expected):
    """The pre-existing MAXIMOR_-prefixed form keeps working after adding the alias."""

    monkeypatch.setenv(env_name, value)
    assert getattr(_settings(), field_name) == expected
