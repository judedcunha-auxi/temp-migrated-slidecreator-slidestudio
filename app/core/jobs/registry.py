"""
SlideForge service: core/jobs/registry. Which job types exist and which handler runs each.

The code that owns a job type registers it here (for example `app.core.storyline.job.register`), with its
dependencies already bound into the handler. The process that runs workers then installs the registry on
its queue and hands the handlers to `Worker`:

    registry = JobRegistry()
    storyline_job.register(registry, model)
    handlers = registry.install(queue)
    Worker(queue, handlers)

Nothing here starts a worker: wiring workers into the app process comes with the first route that enqueues
a job (docs/architecture.md, "Not done yet").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.jobs.queue import JobQueue, JobType
from app.core.jobs.worker import Handler


@dataclass
class JobRegistry:
    types: dict[str, JobType] = field(default_factory=dict)
    handlers: dict[str, Handler] = field(default_factory=dict)

    def add(self, job_type: JobType, handler: Handler) -> None:
        if job_type.name in self.types:
            raise ValueError(f"job type {job_type.name!r} is already registered")
        self.types[job_type.name] = job_type
        self.handlers[job_type.name] = handler

    def install(self, queue: JobQueue) -> dict[str, Handler]:
        """Register every type on `queue`; returns the handlers, keyed by type name, for `Worker`."""
        for job_type in self.types.values():
            queue.register(job_type)
        return dict(self.handlers)
