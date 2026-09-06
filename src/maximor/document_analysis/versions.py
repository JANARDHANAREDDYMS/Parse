"""Centralize stable application-owned document-analysis version labels.

Handlers use these constants for requests, prompts, persistence, and APIs. They do
not derive versions from dates, runtime environment, or the Claude SDK.
"""

DOCUMENT_ANALYSIS_SCHEMA_VERSION = "1.1.0"
DOCUMENT_ANALYSIS_AGENT_VERSION = "0.1.0"
DOCUMENT_ANALYSIS_PROMPT_VERSION = "1.1.0"
ORDER_FORM_ANALYSIS_SKILL_VERSION = "1.2.0"
