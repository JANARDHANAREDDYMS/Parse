"""Decide which candidates get sent to SKU mapping at all.

This is deliberately separate from `build_sku_mapping_task`, which makes no
eligibility judgment of its own — a task can be built for any candidate
regardless of its commercial status. This module owns that separate,
versioned decision, so it can change independently of the task/decision
contracts it gates.
"""

from enum import StrEnum

from maximor.document_analysis.schemas import CommercialStatus

SKU_MAPPING_ELIGIBILITY_POLICY_VERSION = "1.0.0"


class EligibilityDisposition(StrEnum):
    """Classify what should happen to one candidate, not what SKU it maps to.

    `REVIEW` is distinct from `SKIP`: an `ambiguous` commercial status means
    evidence could not resolve whether the candidate was actually purchased
    at all, so it must not be silently dropped the way a confirmed
    `optional`/`excluded`/`mentioned` candidate is — but it also must not be
    auto-mapped as if it were confirmed. Nothing currently acts on `REVIEW`
    beyond withholding automatic scheduling; a human/agent review queue is a
    later concern.
    """

    SCHEDULE = "schedule"
    SKIP = "skip"
    REVIEW = "review"


class SkuMappingEligibilityPolicy:
    """Map one candidate's commercial status to a scheduling disposition.

    Deterministic and versioned rather than configurable per call: changing
    which statuses schedule mapping is a real policy change, not a per-run
    parameter, so it gets its own version constant to track when the rule
    itself changes.
    """

    version = SKU_MAPPING_ELIGIBILITY_POLICY_VERSION

    _DISPOSITIONS: dict[CommercialStatus, EligibilityDisposition] = {
        CommercialStatus.PURCHASED: EligibilityDisposition.SCHEDULE,
        CommercialStatus.INCLUDED: EligibilityDisposition.SCHEDULE,
        CommercialStatus.OPTIONAL: EligibilityDisposition.SKIP,
        CommercialStatus.EXCLUDED: EligibilityDisposition.SKIP,
        CommercialStatus.MENTIONED: EligibilityDisposition.SKIP,
        CommercialStatus.AMBIGUOUS: EligibilityDisposition.REVIEW,
    }

    def evaluate(self, commercial_status: CommercialStatus) -> EligibilityDisposition:
        """Return the disposition for one candidate's commercial status."""

        return self._DISPOSITIONS[commercial_status]
