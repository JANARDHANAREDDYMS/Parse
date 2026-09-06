"""Assert term-applicability reasoning has been removed from the prompt and skill.

Superseded design note: an earlier revision of this file asserted the
*presence* of conservative applicability-classification language in the
prompt/skill. That approach is now fully superseded -- `DocumentAnalysisAgent`
no longer reasons about applicability at all, so this file now asserts its
*absence*, plus that the mandatory grounding/extraction requirements survive
unchanged.

Non-paid: reads only the committed skill file and calls the agent's pure
`_prompt` builder with generated identifiers. No Claude call, no documents.
"""

import uuid

from maximor.config import PROJECT_ROOT
from maximor.document_analysis.agent import ClaudeDocumentAnalysisAgent
from maximor.document_analysis.contracts import DocumentAnalysisRequest

SKILL_PATH = PROJECT_ROOT / ".claude" / "skills" / "order-form-analysis" / "SKILL.md"

# Phrases that only ever appeared in the retired applicability-classification
# instructions -- their presence anywhere would mean reasoning requirements
# leaked back in.
RETIRED_REASONING_PHRASES = (
    "applicability_scope",
    "applies_to_candidate_ids",
    "assign candidate only when",
    "assign document only when",
    "assign `candidate` only when",
    "assign `document` only when",
    "Classify each term's applicability",
    "classify each term's applicability",
    "Classify each term's `applicability_scope`",
)


def _request() -> DocumentAnalysisRequest:
    return DocumentAnalysisRequest(
        organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), preprocessing_schema_version="1",
        document_analysis_schema_version="1", prompt_version="p1",
        skill_version="skill-1", agent_version="a1",
    )


def test_skill_no_longer_contains_term_applicability_reasoning():
    """The committed skill file carries no applicability-classification instructions."""

    text = SKILL_PATH.read_text(encoding="utf-8")
    for phrase in RETIRED_REASONING_PHRASES:
        assert phrase not in text, phrase
    # Raw-term extraction and the "resolved later" hand-off remain explicit.
    normalized = " ".join(text.split())
    assert "extract its raw name and value with cited evidence only" in normalized
    assert "do not omit a term because its meaning or scope is unclear" in normalized
    assert "applicability is resolved later by a separate process" in normalized
    # Mandatory grounding requirements stay unchanged.
    assert "get_document_overview" in text
    assert "search_document" in text


def test_prompt_no_longer_contains_term_applicability_reasoning():
    """The agent's initial prompt carries no applicability-classification instructions."""

    prompt = ClaudeDocumentAnalysisAgent._prompt(_request())
    for phrase in RETIRED_REASONING_PHRASES:
        assert phrase not in prompt, phrase
    assert "Extract every global term's raw name and value with cited evidence" in prompt
    assert "do not omit a term because its meaning or scope is unclear" in prompt
    assert "a separate later process resolves applicability" in prompt
    # Mandatory grounding requirements stay unchanged.
    assert "get_document_overview first" in prompt and "content-retrieval tool" in prompt
    # No document content, storage paths, SQL, or secrets ever appear in the prompt.
    assert "storage_key" not in prompt and "SELECT " not in prompt
