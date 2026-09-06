"""Test the deterministic SKU-mapping eligibility policy: no database, no Claude call."""

from maximor.document_analysis.schemas import CommercialStatus
from maximor.sku_mapping.eligibility import (
    SKU_MAPPING_ELIGIBILITY_POLICY_VERSION,
    EligibilityDisposition,
    SkuMappingEligibilityPolicy,
)


def test_purchased_and_included_schedule_mapping():
    policy = SkuMappingEligibilityPolicy()
    assert policy.evaluate(CommercialStatus.PURCHASED) is EligibilityDisposition.SCHEDULE
    assert policy.evaluate(CommercialStatus.INCLUDED) is EligibilityDisposition.SCHEDULE


def test_optional_excluded_and_mentioned_are_skipped():
    policy = SkuMappingEligibilityPolicy()
    assert policy.evaluate(CommercialStatus.OPTIONAL) is EligibilityDisposition.SKIP
    assert policy.evaluate(CommercialStatus.EXCLUDED) is EligibilityDisposition.SKIP
    assert policy.evaluate(CommercialStatus.MENTIONED) is EligibilityDisposition.SKIP


def test_ambiguous_is_routed_to_review_not_scheduled_or_silently_dropped():
    policy = SkuMappingEligibilityPolicy()
    disposition = policy.evaluate(CommercialStatus.AMBIGUOUS)
    assert disposition is EligibilityDisposition.REVIEW
    assert disposition is not EligibilityDisposition.SCHEDULE
    assert disposition is not EligibilityDisposition.SKIP


def test_policy_covers_every_commercial_status():
    """Fail loudly if a future CommercialStatus value has no defined disposition."""

    policy = SkuMappingEligibilityPolicy()
    for status in CommercialStatus:
        assert policy.evaluate(status) in EligibilityDisposition


def test_policy_is_versioned():
    assert SkuMappingEligibilityPolicy.version == SKU_MAPPING_ELIGIBILITY_POLICY_VERSION
    assert SKU_MAPPING_ELIGIBILITY_POLICY_VERSION == "1.0.0"
