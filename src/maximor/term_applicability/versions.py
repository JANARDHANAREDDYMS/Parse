"""Centralize stable application-owned term-applicability version labels.

Callers use these constants for task contracts, decision output, prompts, and
the agent itself. They are never derived from dates, runtime environment, or
an SDK response.
"""

TERM_APPLICABILITY_TASK_SCHEMA_VERSION = "1.0.0"
TERM_APPLICABILITY_DECISION_SCHEMA_VERSION = "1.0.0"
# Bumped for the addition of `candidate_commercial_facts`: old term-only
# results (schema_version "1.0.0") remain loadable via plain deserialization
# (the new field defaults to an empty tuple) but are no longer accepted as a
# *current* live submission by `validate_term_applicability_result`.
TERM_APPLICABILITY_RESULT_SCHEMA_VERSION = "1.2.0"
# Bumped for the removal of the redundant `extracted_fields` finalizer
# submission requirement: the canonical `CandidateCommercialFactCoverage`
# shape itself is unchanged (old persisted JSON with an explicit
# `extracted_fields` still loads), but a *live* finalizer submission that
# still includes it is now rejected as a schema/content mismatch.
TERM_APPLICABILITY_SKILL_VERSION = "1.2.0"
TERM_APPLICABILITY_PROMPT_VERSION = "1.3.0"
TERM_APPLICABILITY_AGENT_VERSION = "0.4.0"
