# Maximor AI Take-Home Notes

Use this file as a shared notebook for decisions, questions, experiments, and progress.

## Working Agreement

- Maintain architectural consistency across the discussion and implementation.
- Do not silently replace an earlier hybrid or agent-based decision with a deterministic-only design, or vice versa.
- When refining a decision, explicitly state the previous decision, the proposed change, and the reason for changing it before treating the new design as settled.
- Distinguish clearly between a simplified explanation and the complete architecture so simplification is not mistaken for a design change.
- Keep an explicit decision log for settled architectural choices.

## Project Goal

Build an agentic order-form parsing system that:

- Accepts an order-form PDF and a known SKU catalog.
- Extracts contract-level metadata and purchased contract items.
- Maps every extracted item to a valid catalog SKU.
- Produces structured JSON.
- Validates its output and measures accuracy against supplied ground truth.

## Data Overview

- 50 synthetic order-form PDFs, each two pages long.
- 50 preview-text files.
- 50 ground-truth JSON files.
- 21 catalog SKUs.
- 1 JSONL manifest connecting each PDF to its ground truth.
- 132 ground-truth contract items in total.

### SKU Catalog

Each SKU contains:

- `id`: Internal UUID.
- `sku_code`: Stable business identifier.
- `name`: Official product name.
- `description`: Product or service meaning.
- `parsing_instructions`: SKU-specific interpretation rules.
- `usage_count`: Undocumented metadata; it does not equal occurrences in these 50 forms.

### Ground Truth

Each `ground_truth/of-XXXX.json` file is the expected structured output for the matching `pdfs/of-XXXX.pdf` file. It is used to measure SKU identification and field-extraction accuracy.

The preview-text files are debugging aids, not ground truth and not intended as production parser inputs.

### Manifest

Each manifest record contains:

- `form_id`
- `quote_number`
- `pdf_path`
- `ground_truth_path`
- `contract_item_count`

The `/app/...` paths are stale for this machine and should be resolved relative to the local dataset directory. `contract_item_count` reveals part of the expected answer, so it should be used by the evaluation harness, not passed to the parsing agent.

## Current Architecture Direction

We havent finalised it yet.

## Learning and Evaluation

- The LLM does not automatically update its model weights from ground truth.
- The workflow can adapt by improving prompts, aliases, validation rules, examples, and error memory.
- Development examples may be used for that adaptation.
- A held-out test set must remain unseen until the workflow is frozen to avoid data leakage.


## Important Findings

- PDFs contain extractable text but include deliberate OCR-like substitutions such as `1` in place of `i`.
- The dataset ground-truth schema is narrower than the assignment description.
- Ground truth does not include fields such as `discount` or `unit_price_period`.
- The benchmark-compatible schema should match ground truth, while broader assignment fields can be optional extensions.
- Catalog `usage_count` values total 251, whereas the 50 forms contain 132 line items. Treat `usage_count` as untrusted auxiliary metadata.

## Open Questions

- Which Claude model and document-input method should be used?
- Should the primary parser use embedded PDF text, document vision, or a hybrid?
- What exact evaluation metrics and matching rules should define accuracy?
- How should repeated items with the same SKU be matched during evaluation?
- Should adaptive memory persist across runs or only generate proposed prompt/rule changes?

## Work Log

### Initial data review

- Confirmed that the local assignment PDF matches the pasted brief.
- Confirmed completeness of PDFs, preview text, ground truth, catalog, and manifest.
- Confirmed that the Notion page requires Maximor workspace authentication.
- Confirmed that the workspace has not yet been initialized as a Git repository.

## Personal Notes

Optimisation to keep note of:
Using different models could later be an optimization—for example, an expensive model for extraction and a cheaper model for validation—but it adds complexity that probably does not improve the initial submission.

Anthropic’s current documentation says tool-selection accuracy can degrade once more than approximately 30–50 tools are available simultaneously. It recommends tool search for large catalogs so only a few relevant definitions are loaded. Anthropic tool-search documentation

Should we copy Anthropic’s PDF skill?
We should use it as a design reference, but not copy it wholesale without checking its license. Its frontmatter identifies it as proprietary, and Anthropic describes its document skills as source-available rather than open source.
Also, it is intentionally general-purpose. It includes merging, splitting, rotation, watermarking, encryption and PDF creation—capabilities our parsing agent does not need.
so, i am using only parsing after reading the whole skills.md

One important limitation: do not run long PDF jobs using FastAPI’s simple in-process background tasks. FastAPI’s documentation recommends a separate worker system for heavy background computation. FastAPI background-task guidance


Agents with more deterministic rules work better, timestamp of video 1:09

How to handle context in agents so that we dont get diminishing returns. Say UX design, see video time stamp 1:17:00

The runner and dispatcher are ordinary Python components inside the same Python worker. Neither is an agent or separate server. The runner controls the lifecycle of background jobs, The dispatcher answers one simple question:
Which handler should execute this type of job? The dispatcher does not do the work, the Handler performs the actual job-specific work.

For your current implementation, making every internal tool an MCP server immediately would add:
- Another server process
- Transport configuration
- Tool serialization
- Authentication and authorization
- Deployment complexity
- More integration testing
Your narrow tools currently live in the same Python backend. Direct Python tool adapters are simpler and safer for the take-home. You can expose them through MCP later without redesigning the underlying services.

Do not introduce MCP yet.
Your internal structure should still allow MCP later:


## scalable end to end arch for now:

Client/API
    ↓
Input validation and authentication
    ↓
Object storage for PDFs
    ↓
Job queue
    ↓
Document preprocessing worker
    ├── Embedded text
    ├── Layout blocks
    ├── Page images
    └── OCR fallback
    ↓
DocumentAnalysisAgent
│
├── Skill: order-form-analysis
│     └── Workflow instructions and domain procedures
│
├── Narrow tools
│     ├── extract_pdf_text
│     ├── extract_pdf_layout
│     ├── extract_pdf_tables
│     ├── render_pdf_page
│     ├── run_page_ocr
│     ├── search_document
│     └── save_document_analysis
│
└── Output
      ├── contract structure
      ├── pricing sections
      ├── global terms
      ├── product candidates
      ├── commercial statuses
      ├── raw contract-item attributes
      └── evidence references
    ↓
SKU-MAPPING SUB SYSTEM AGENT
│
├── Input
│     └── Eligible ProductCandidates from DocumentAnalysisAgent
│
├── Input checks
│     └── Verify candidate, evidence, organization and catalog
│
├── Mapping records
│     └── Track one mapping decision per ProductCandidate
│
├── SKU-mapping skill
│     └── Teaches Claude the mapping procedure and restrictions
│
├── SkuRepository
│     ├── JsonSkuRepository for the take-home
│     └── PostgresSkuRepository for production
│
├── HybridSkuRetriever
│     ├── Exact search
│     ├── Lexical/trigram search
│     ├── pgvector semantic search
│     └── Merged ranked results
│
├── SkuMappingAgent
│     ├── Calls narrow tools
│     ├── Examines retrieved SKUs and evidence
│     └── Returns MATCH / NO_MATCH / AMBIGUOUS
│
├── Mapping validation
│     └── Confirms schema, catalog membership and evidence
│
├── Targeted mapping retries
│     ├── Add document evidence
│     ├── Broaden retrieval text
│     └── Correct invalid mapping output
│
├── Mapping persistence
│     └── Store retrievals, attempts, validations and decisions
│
└── Output
      └── Validated SKU mapping for each ProductCandidate
    ↓
Normalization
│
├── Deterministic normalization
│     ├── Money and currency
│     ├── Dates
│     ├── Quantities
│     ├── Payment terms
│     └── Billing enums
│
└── LLM assistance only for semantic ambiguity
    ↓
Final validation
│
├── Deterministic checks
│     ├── Output schema
│     ├── Decimal arithmetic
│     ├── Date consistency
│     ├── SKU integrity
│     └── Total reconciliation
│
└── LLM semantic review
      ├── Evidence supports the value
      ├── Shared terms were applied correctly
      └── Conflicting clauses were interpreted correctly
    ↓
Targeted correction
│
├── Document or attribute problem
│     └── Return only that problem to DocumentAnalysisAgent
│
└── SKU-mapping problem
      └── Return only that problem to SkuMappingAgent
    ↓
Run normalization and validation again
    ↓
Final schema construction
    ↓
finalize_extraction
│
├── Save COMPLETED when every required check passes
└── Otherwise save REVIEW_REQUIRED or FAILED_VALIDATION
    ↓
Result database/API response

The repository and retriever are not themselves inside Claude. They are tools/services available to the SkuMappingAgent.


## Product Scale Decisions to consider:

Multi-tenancy
Every catalog lookup must be filtered by organization_id. One organization’s SKU must never appear in another organization’s results.


Asynchronous processing
Large PDFs should run as jobs:

Idempotency
Re-uploading the same request should not create duplicate extractions or duplicate API charges. Use document hashes and request idempotency keys.

Caching
Cache:
- PDF extraction results
- Page images
- OCR results
- SKU embeddings
- Unchanged catalog retrieval results
Do not blindly cache final answers across catalog versions.


Security
Order forms contain commercially sensitive information. The product should include:
- Encryption in transit and at rest
- Tenant-isolated storage
- Short-lived document access
- Audit logs
- Configurable retention and deletion
- Secrets stored outside source control
- No contract text in ordinary application logs


Observability
Record per job:
- Processing duration
- LLM calls
- Tokens and estimated cost
- Validation failures
- Correction attempts
- Retrieval candidates and scores
- Final confidence
- Model and prompt versions
- Catalog and schema versions
This is essential for improving the system safely.



| Component | Appropriate LLM use |
|---|---|
| Input validation | Detecting whether an uploaded document is actually an order form |
| PDF processing | Repairing corrupted OCR or interpreting visually complex tables |
| Document understanding | Primary LLM responsibility |
| Candidate detection | Primary LLM responsibility |
| Purchased-status classification | Primary LLM responsibility |
| SKU mapping | Primary LLM responsibility |
| Normalization | Ambiguous semantic values only |
| Validation | Semantic evidence and contradiction checks |
| Correction | Reinterpreting targeted fields |
| Ground-truth error analysis | Explaining prediction failures and proposing improvements |
| Human-review preparation | Producing concise explanations and highlighting relevant clauses |


## Fixed Architecture Principle: Narrow Typed Tools

Agents must not receive unrestricted Bash, arbitrary SQL, unrestricted filesystem access, or a generic database-write capability. Each agent receives only narrowly defined, typed tools required for its role.

Initial tool contracts:

- `search_document(...)`
- `retrieve_skus(...)`
- `get_authoritative_sku(...)`
- `normalize_money(...)`
- `normalize_date(...)`
- `calculate_line_total(...)`
- `reconcile_document_total(...)`
- `validate_extraction(...)`
- `finalize_extraction(...)`

The hybrid SKU retrieval subsystem must follow the same principle. Exact, lexical/trigram, and pgvector searches are controlled retrieval operations scoped by `organization_id` and `catalog_version_id`. The `SkuMappingAgent` should normally receive a single `retrieve_skus(...)` tool plus authoritative lookup, rather than arbitrary database or SQL access.

The LLM proposes interpretations. Typed deterministic tools perform retrieval, transformations, arithmetic, validation, and persistence.

### Narrow Document Tools Reduce Token Usage

The complete lossless preprocessing result remains in object storage and PostgreSQL for
traceability, but `DocumentAnalysisAgent` must not receive the entire stored document
representation on every Claude call. It should begin with a compact document and page
inventory, then use narrow typed tools to retrieve only the relevant page text, layout
blocks, tables, OCR text, evidence regions, or page renders.

Compression reduces storage and transfer size only. It does not reduce model tokens after
the content is decompressed. Selective retrieval through narrow tools is what controls LLM
context size, repeated input-token cost, and irrelevant-document noise while preserving all
representations in authoritative storage.

### Guarded Finalization

`finalize_extraction(...)` is the only tool allowed to persist an accepted final result. It must refuse to save a result with status `COMPLETED` unless all required deterministic checks pass:

- Output conforms to the versioned JSON schema.
- Every selected SKU exists in the authoritative catalog.
- SKU ID, code, and name agree.
- SKU belongs to the correct organization and catalog version.
- Required evidence references point to stored document pages or blocks.
- Required dates are valid and temporally consistent.
- Monetary values use decimal-safe normalization.
- Required arithmetic and reconciliation checks pass within an explicit tolerance.
- Enum values are allowed.
- No unresolved ambiguity remains for a required field.

Failed attempts may still be stored for audit and debugging, but only with a non-accepted status such as `FAILED_VALIDATION` or `REVIEW_REQUIRED`.

This is a settled design decision. Any future proposal to broaden tool permissions or bypass guarded finalization must be called out explicitly before implementation.
