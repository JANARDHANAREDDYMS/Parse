"""Centralize stable application-owned term-triage version labels.

Callers use these constants for task contracts, decision output, prompts, and
the agent itself. They are never derived from dates, runtime environment, or
an SDK response.
"""

TERM_TRIAGE_TASK_SCHEMA_VERSION = "1.0.0"
TERM_TRIAGE_RESULT_SCHEMA_VERSION = "1.0.0"
TERM_TRIAGE_SKILL_VERSION = "1.0.0"
TERM_TRIAGE_PROMPT_VERSION = "1.0.0"
TERM_TRIAGE_AGENT_VERSION = "0.1.0"
