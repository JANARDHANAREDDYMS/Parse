"""Confirm term_applicability reuses document_analysis's shared types, never redefines them."""

from maximor.document_analysis import schemas as document_analysis_schemas
from maximor.term_applicability import contracts as term_applicability_contracts
from maximor.term_applicability import schemas as term_applicability_schemas


def test_applicability_scope_is_the_same_object_as_document_analysis():
    assert term_applicability_schemas.ApplicabilityScope is document_analysis_schemas.ApplicabilityScope


def test_evidence_reference_is_the_same_object_as_document_analysis():
    assert term_applicability_schemas.EvidenceReference is document_analysis_schemas.EvidenceReference


def test_identifier_is_the_same_object_as_document_analysis():
    assert term_applicability_schemas.Identifier is document_analysis_schemas.Identifier


def test_commercial_status_is_the_same_object_as_document_analysis():
    assert term_applicability_contracts.CommercialStatus is document_analysis_schemas.CommercialStatus


def test_document_analysis_result_is_the_same_object_as_document_analysis():
    assert term_applicability_contracts.DocumentAnalysisResult is document_analysis_schemas.DocumentAnalysisResult
