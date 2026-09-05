"""Specify the async handler boundary from an immutable job context to completion."""

from typing import Protocol

from maximor.jobs.types import JobContext


class JobHandler(Protocol):
    """Execute one job or raise; final database status remains runner-owned."""

    async def execute(self, context: JobContext) -> None:
        """Perform handler-specific verification without changing job status."""

        ...
