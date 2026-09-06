"""Centralize stable application-owned SKU-mapping version labels.

Callers use these constants for task contracts, retrieval output, prompts,
and the agent itself. They are never derived from dates, runtime environment,
or an SDK response.
"""

SKU_MAPPING_TASK_SCHEMA_VERSION = "1.0.0"
SKU_RETRIEVAL_SCHEMA_VERSION = "1.0.0"
HYBRID_SKU_RETRIEVER_VERSION = "0.1.0"
SKU_MAPPING_DECISION_SCHEMA_VERSION = "1.0.0"
SKU_MAPPING_SKILL_VERSION = "1.0.0"
SKU_MAPPING_PROMPT_VERSION = "1.0.0"
SKU_MAPPING_AGENT_VERSION = "0.1.0"
