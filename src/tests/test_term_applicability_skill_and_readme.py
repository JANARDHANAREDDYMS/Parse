"""Confirm the term-applicability skill and README document the settled safeguards.

Non-paid: reads only the committed skill and README files. No Claude call, no
documents.
"""

from maximor.config import PROJECT_ROOT

SKILL_PATH = PROJECT_ROOT / ".claude" / "skills" / "term-applicability" / "SKILL.md"
README_PATH = PROJECT_ROOT / "src" / "maximor" / "term_applicability" / "README.md"


def _normalized(path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_readme_documents_evidence_id_only_finalizer_resolution():
    text = _normalized(README_PATH)
    assert "evidence IDs only" in text
    assert "resolves them to exact trusted `EvidenceReference` objects" in text


def test_readme_documents_trusted_identity_injection():
    text = _normalized(README_PATH)
    assert "trusted and injected server-side" in text
    assert "Claude cannot override them" in text


def test_readme_documents_exact_namespaced_tool_allowlist():
    text = _normalized(README_PATH)
    assert "exact namespaced tool" in text
    assert "never bare operation names" in text


def test_readme_documents_narrow_retrieval_not_exhaustive_comparison():
    text = _normalized(README_PATH)
    assert "must never load whole preprocessing artifacts" in text
    assert "must never compare every term against every candidate" in text


def test_readme_documents_unknown_as_preferred_outcome():
    text = _normalized(README_PATH)
    assert "`unknown` is a valid, **preferred** outcome" in text


def test_readme_documents_scope_exclusions():
    text = _normalized(README_PATH)
    assert "does not select SKUs, normalize values, do arithmetic, or alter" in text


def test_skill_documents_evidence_id_only_submission():
    text = _normalized(SKILL_PATH)
    assert "Submit evidence by ID only" in text
    assert "you do not need to (and cannot) reconstruct" in text


def test_skill_documents_unknown_as_preferred_and_no_exhaustive_comparison():
    text = _normalized(SKILL_PATH)
    assert "unknown` is a correct, preferred outcome" in text
    assert "do not compare every term against every candidate" in text


def test_skill_documents_selected_terms_only_and_no_metadata_classification():
    """The skill now receives only triage-selected terms; it no longer classifies metadata."""

    text = _normalized(SKILL_PATH)
    assert "already been excluded from this batch" in text
    assert "do not need to classify anything outside the" in text


def test_skill_requires_retrieval_before_affirmative_scope_claim():
    text = _normalized(SKILL_PATH)
    assert "Before deciding `document` or `candidate` scope for a term, call" in text
    assert "unknown` scope does not require a retrieval call" in text
    assert "Before naming any candidate in a `candidate`-scope decision, call" in text


def test_skill_uses_correct_finalizer_tool_name():
    """The skill must name the tool the agent actually registers, not a stale alias."""

    text = _normalized(SKILL_PATH)
    assert "finalize_term_applicability" in text
    assert "submit_term_applicability_result" not in text


def test_skill_documents_trusted_identifiers_not_settable_by_model():
    text = _normalized(SKILL_PATH)
    assert "are supplied for you and are not yours to set or change" in text


def test_skill_excludes_sku_selection_and_normalization():
    text = _normalized(SKILL_PATH)
    assert "Do not select a SKU, parse or normalize dates or money" in text
