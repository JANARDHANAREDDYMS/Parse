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
from maximor.term_applicability.agent import ClaudeTermApplicabilityAgent
from maximor.term_triage.agent import ClaudeTermTriageAgent


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


def test_term_agent_settings_have_safe_defaults_and_agents_construct():
    """Both term agents can resolve every centralized setting from defaults."""

    configured = _settings()
    assert configured.term_triage_model == "claude-sonnet-5"
    assert configured.term_triage_max_turns == 4
    assert configured.term_triage_max_thinking_tokens == 1024
    assert configured.term_triage_timeout_seconds == 60
    assert configured.term_triage_max_corrections == 1
    assert configured.term_applicability_model == "claude-sonnet-5"
    assert configured.term_applicability_max_turns == 8
    assert configured.term_applicability_max_thinking_tokens == 2048
    assert configured.term_applicability_timeout_seconds == 150
    assert configured.term_applicability_max_corrections == 1
    ClaudeTermTriageAgent(configured)
    ClaudeTermApplicabilityAgent(configured)


@pytest.mark.parametrize(
    ("field_name", "bare_name", "prefixed_name", "value", "expected"),
    [
        ("term_triage_timeout_seconds", "TERM_TRIAGE_TIMEOUT_SECONDS", "MAXIMOR_TERM_TRIAGE_TIMEOUT_SECONDS", "75", 75),
        ("term_applicability_timeout_seconds", "TERM_APPLICABILITY_TIMEOUT_SECONDS", "MAXIMOR_TERM_APPLICABILITY_TIMEOUT_SECONDS", "150", 150),
        ("term_triage_max_turns", "TERM_TRIAGE_MAX_TURNS", "MAXIMOR_TERM_TRIAGE_MAX_TURNS", "6", 6),
        ("term_applicability_max_thinking_tokens", "TERM_APPLICABILITY_MAX_THINKING_TOKENS", "MAXIMOR_TERM_APPLICABILITY_MAX_THINKING_TOKENS", "4096", 4096),
    ],
)
def test_term_agent_settings_accept_bare_and_prefixed_aliases(monkeypatch, field_name, bare_name, prefixed_name, value, expected):
    """Both documented environment-variable spellings configure each field."""

    monkeypatch.setenv(bare_name, value)
    assert getattr(_settings(), field_name) == expected
    monkeypatch.delenv(bare_name)
    monkeypatch.setenv(prefixed_name, value)
    assert getattr(_settings(), field_name) == expected
