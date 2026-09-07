# Normalization subsystem

This now covers **Stages 1–5A of the normalization pipeline**: trusted input,
deterministic primitive normalization, provisional line-item assembly,
conservative inheritance/reconciliation, and finalization readiness.
trusted-input assembly only. No money/date/quantity normalization, no
arithmetic, no correction loop, no job type, no worker handler, no API
endpoint, and no Claude call exist yet -- nothing here is reachable from a
live upload.

```text
Completed document analysis
+ completed SKU mappings
+ completed term enrichment
        |
        v
Trusted NormalizationInput
        |
        v
Candidate/source bundles (LineItemSourceBundle)
        |
        v
Provisional normalized extraction and finalization readiness
```

## Why this is a separate stage

Every upstream subsystem (`document_analysis`, `sku_mapping`,
`term_applicability`) already enforces its own guarded finalization and
persistence. Normalization must consume those *already-resolved* results,
not re-derive or second-guess them -- this stage's only job is to gather
them into one trusted, internally consistent bundle per document, and fail
loudly (never silently) when an eligible candidate is missing something a
later normalizer would need.

## Components

| Component | Status | Input | Output |
|---|---|---|---|
| `NormalizedOrderMetadata` / `NormalizedLineItem` / `MoneyValue` / `PriceScheduleEntry` / `FieldProvenance` / `FinalOrderFormExtraction` | **Contracts only** | — | — |
| `NormalizationRequest` | **Implemented** | organization/document/analysis-run/term-applicability-run IDs | an identifier bundle, not yet loaded |
| `NormalizationInput` / `assemble_normalization_input` | **Implemented** | one completed `DocumentAnalysisResult` + combined term-applicability artifact + per-candidate `SkuMappingRunArtifact` | a validated `NormalizationInput`, or a typed source error |
| `LineItemSourceBundle` / `assemble_line_item_source_bundles` | **Implemented** | one `NormalizationInput` | one bundle per eligible candidate with a `MATCH` SKU mapping |
| `NormalizationRepository.load_normalization_input` | **Implemented** | tenant/document/analysis-run/term-applicability-run IDs | a trusted `NormalizationInput`, loaded through the existing `document_analysis`/`term_applicability`/`sku_mapping` persistence services |
| Deterministic primitive normalizers / provisional line-item assembly | **Implemented (Stages 2–3)** | `LineItemSourceBundle` | review-required draft |
| Exact-match document-term inheritance / full-order validation | **Implemented (Stage 4)** | trusted input + provisional draft | inherited draft + deterministic `ValidationReport` |
| Finalization readiness / final-schema-shaped candidate | **Implemented (Stage 5A)** | provisional extraction + trusted input | candidate + `FinalizationReadiness` (`READY_FOR_SEMANTIC_REVIEW`, `REVIEW_REQUIRED`, or `FAILED_VALIDATION`) |
| Bounded semantic review contracts / guarded agent | **Implemented (Stage 5B)** | eligible Stage 5A review items | ordered evidence-backed semantic findings |
| Semantic review, correction loop, `COMPLETED` semantics, persistence/job/API wiring | **Not built (Stage 5B+)** | — | — |

## Scope (settled, not to be silently widened)

This stage answers exactly one question: *given everything already decided
upstream, what is the complete, trustworthy set of inputs one candidate's
eventual line item will be normalized from?* It does not parse a date,
compute a `Decimal`, resolve a currency, reconcile a total, or decide
whether a document-wide term should apply to a given line item -- inheriting
a document-wide term onto a line item is explicitly a later stage's
decision, which is why `LineItemSourceBundle` exposes
`available_document_terms` rather than merging them in.

## Eligibility and completeness rules (settled)

- Only `purchased`/`included` candidates are eligible for a future line
  item, mirroring `FACT_ELIGIBLE_COMMERCIAL_STATUSES` in
  `term_applicability.contracts`. `excluded`/`optional`/`mentioned`/
  `ambiguous` candidates never produce a `LineItemSourceBundle`, regardless
  of what evidence exists for them.
- Every eligible candidate **must** resolve to a completed SKU-mapping run
  (any outcome) and, if it has expected raw-fact hints (`qty`, `unit_price`,
  `total_contract_value` present in its raw attributes), a completed
  `CandidateCommercialFactCoverage` declaration. Either being absent raises a
  typed `maximor.normalization.errors` exception during assembly -- an
  eligible candidate can never silently disappear.
- Only a `MATCH` SKU-mapping outcome produces a `LineItemSourceBundle`. A
  `NO_MATCH`/`AMBIGUOUS` outcome is not an assembly error (it is a complete,
  honest answer already captured in `NormalizationInput.sku_mappings`) --
  it simply does not yield a line item.
- `unknown` term-applicability decisions and unresolved commercial fields
  are carried through unchanged, exactly as persisted, for later
  validation/review -- this stage does not resolve or discard them.

## Provenance model (settled)

`FieldProvenance` records one of four `ProvenanceSourceType` values
(`candidate_fact`, `document_term`, `sku_mapping`, `derived`) plus a
`ValueOrigin` (`extracted`, `inherited`, `derived`), the specific upstream
record identity that source type implies, and bounded evidence references
only -- never raw document bodies, prompts, or SQL. `NormalizedOrderMetadata`
and `NormalizedLineItem` each carry one `field_provenance` dict keyed by
their own field names, rather than one parallel `*_provenance` field per
value.

## Not yet decided (Stage 2+)

Primitive parsing is conservative: explicit currency and unambiguous dates are
required; Decimal arithmetic uses half-even rounding to two places only for
the explicitly supported quantity × unit-price derivation. Supplied totals are
compared to calculated totals and conflicts become structured review issues.
Missing values remain absent; no quantity, currency, or period defaults are invented.
Document-wide terms are kept available but are not inherited onto line items.

Inheritance uses a small exact, case/whitespace-tolerant alias registry only;
candidate facts win over candidate-scoped terms, which win over document-scoped
terms. Unknown and metadata terms are never inherited. Validation records
conflicts and multiple-currency observations without repairing them.

Stage 5A never emits `COMPLETED`: a clean deterministic result is only
`READY_FOR_SEMANTIC_REVIEW`; missing/ambiguous/conflicting values are
`REVIEW_REQUIRED`, while broken lineage, SKU identity, provenance, or other
hard invariants are `FAILED_VALIDATION`. Review issues carry stable queue
classifications and deterministic locations without source content.

Semantic review for bounded ambiguity, targeted correction, final
`COMPLETED` semantics, persistence/job/worker/API wiring, and end-to-end
evaluation remain deferred.

Stage 5B sends only `AMBIGUOUS_VALUE`, `SEMANTIC_SCOPE_REQUIRED`, and selected
evidence-supported `CONFLICTING_VALUES` items. Missing values, unsupported
formats, arithmetic/reconciliation differences, and failed-validation issues
remain deterministic. The semantic reviewer is read-only and can recommend a
correction, but cannot apply one.
