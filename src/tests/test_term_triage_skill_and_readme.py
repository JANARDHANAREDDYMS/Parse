"""Confirm the term-triage skill and README document the settled safeguards.

Non-paid: reads only the committed skill and README files. No Claude call, no
documents.
"""

from maximor.config import PROJECT_ROOT

SKILL_PATH = PROJECT_ROOT / ".claude" / "skills" / "term-triage" / "SKILL.md"
README_PATH = PROJECT_ROOT / "src" / "maximor" / "term_triage" / "README.md"


def _normalized(path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_skill_documents_no_evidence_tools_and_no_applicability_claim():
    text = _normalized(SKILL_PATH)
    assert "You have no evidence tools" in text
    assert "no applicability claim" in text


def test_skill_prefers_uncertain_over_forced_metadata():
    text = _normalized(SKILL_PATH)
    assert "Prefer `uncertain` over forcing a confident `document_metadata` label" in text


def test_skill_lists_conservative_terminology_categories():
    text = _normalized(SKILL_PATH)
    for category in ("service period", "billing/invoicing frequency", "payment terms", "currency", "quantity", "pricing", "discount", "tax", "renewal", "order dates"):
        assert category in text


def test_skill_forbids_no_keyword_only_mechanism_language_absent_and_no_scope_inference():
    text = _normalized(SKILL_PATH)
    assert "Do not infer applicability scope, decide which candidate" in text


def test_skill_requires_classifying_every_term_and_concise_rationale():
    text = _normalized(SKILL_PATH)
    assert "Classify every term exactly once" in text
    assert "Keep rationale concise" in text


def test_readme_documents_metadata_is_not_discarded():
    text = _normalized(README_PATH)
    assert "never \"discarded\"" in text or "never `discarded`" in text or "not \"discarded\"" in text


def test_readme_documents_no_keyword_only_filtering():
    text = _normalized(README_PATH)
    assert "must never use keyword filtering as its sole decision mechanism" in text


def test_readme_documents_selection_rule():
    text = _normalized(README_PATH)
    assert "selects every `potential_line_item` and every `uncertain` term" in text
    assert "not automatically selected" in text
