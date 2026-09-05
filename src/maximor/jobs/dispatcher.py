"""Resolve a typed claimed-job context to one registered handler and invoke it."""

from collections.abc import Mapping

from maximor.jobs.contracts import JobHandler
from maximor.jobs.errors import UnsupportedJobTypeError
from maximor.jobs.types import JobContext, JobType


class JobDispatcher:
    """Dispatch one context without querying, committing, or changing job status."""

    def __init__(self, handlers: Mapping[JobType, JobHandler]) -> None:
        self._handlers = dict(handlers)

    @property
    def registered_job_types(self) -> tuple[JobType, ...]:
        """Return job types the runner may safely claim from PostgreSQL."""

        return tuple(self._handlers)

    def resolve(self, job_type: str) -> JobHandler:
        """Return the one registered handler or raise an explicit typed failure."""

        try:
            typed_job_type = JobType(job_type)
            return self._handlers[typed_job_type]
        except (ValueError, KeyError):
            raise UnsupportedJobTypeError(job_type) from None

    async def dispatch(self, context: JobContext) -> None:
        """Invoke exactly one resolved handler and propagate its result or failure."""

        await self.resolve(context.job_type).execute(context)
