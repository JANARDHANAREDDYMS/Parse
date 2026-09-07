"""Centralize stable application-owned normalization version labels.

Handlers use these constants for trusted-input assembly and the eventual
final-output contracts. They do not derive versions from dates, runtime
environment, or the Claude SDK -- there is no Claude SDK in this stage.
"""

# Version of `NormalizationInput` -- the trusted, assembled bundle of
# already-completed document-analysis, term-applicability, and SKU-mapping
# results that a future deterministic normalizer will consume.
NORMALIZATION_INPUT_SCHEMA_VERSION = "0.1.0"

# Version of `LineItemSourceBundle` -- one candidate's grouped sources
# (product candidate, accepted SKU mapping, commercial facts/coverage,
# applicable terms, evidence) assembled from a `NormalizationInput`.
LINE_ITEM_SOURCE_BUNDLE_SCHEMA_VERSION = "0.1.0"

# Version of `FinalOrderFormExtraction` and its nested draft contracts
# (`NormalizedOrderMetadata`, `NormalizedLineItem`). This stage defines the
# shape only -- no normalizer populates it yet.
FINAL_ORDER_FORM_SCHEMA_VERSION = "0.1.0"
INHERITANCE_POLICY_VERSION = "0.1.0"
NORMALIZATION_VALIDATION_POLICY_VERSION = "0.1.0"

# Version of the deterministic finalization-readiness gate.  This is kept
# separate from the normalized output schema because readiness is an
# operational outcome, not a semantic field in the final extraction.
FINALIZATION_POLICY_VERSION = "0.1.0"
NORMALIZATION_RESULT_SCHEMA_VERSION = "0.1.0"
