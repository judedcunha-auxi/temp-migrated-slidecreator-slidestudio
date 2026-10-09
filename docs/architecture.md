# Architecture

How the service is put together. Each area has its own section, written by whoever builds it.
Add a section rather than editing another area's. The plain-English tour is
[how-it-works.md](how-it-works.md); this page is the design.

## Storage and jobs (Phase 4)

### The rule

The service keeps no lasting data of its own. Repo standards: "No direct connection to SQL
Server or blob storage. All data goes through the General service."

- **Redis** holds only what can be lost and rebuilt: the job queue, the in-flight limiter and
  the counters (D6).
- **Decks, brands, files and job records** go through one port to the General service.

The General service does not exist yet (D5). So the port is defined and tested now, and its
real adapter is written when the .NET repo arrives.
[general-service-requirements.md](general-service-requirements.md) is the hand-over spec.

### The storage port

```
app/core/storage/
  ports.py            the protocols (one per area of the plan's §2.2) + the errors
  models.py           the entities, from the Supabase schema + the Netlify Blob shapes
  reference.py        the port semantics, written once, over a RecordBackend
  memory.py           STORAGE_BACKEND=fake   reference over dicts
  local_fs.py         STORAGE_BACKEND=local  reference over files under SCRATCH_ROOT
  general_service.py  STORAGE_BACKEND=general  STUB + the endpoint requirements as data
  factory.py          settings -> Storage
  paths.py            safe_join: the only way a path is built
  workspace.py        a scratch directory per job, backed by the port
```

**Ports.** `Storage` bundles thirteen ports:

- users;
- orgs;
- brands;
- masters;
- decks;
- slides;
- blobs;
- jobs;
- exports;
- usage;
- transcripts;
- analytics;
- health.

Every operation takes a `CallerContext` (identity subject, request id, and whether it is a
system call). An adapter passes these on to the General service for authorisation, audit and
log correlation.

**Errors.** The errors belong to the port: `NotFound`, `Forbidden`, `Conflict`, `InvalidInput`
and `Unavailable`. A route maps them to its own contract. For example, Darwin answers 403 "Not
your deck" for a `NotFound` deck.

**One set of semantics, three backends.** The in-memory store and the local adapter are the
same `ReferenceStorage` over two `RecordBackend`s. So the following behave identically in
both:

- ownership;
- the org-brand ACL;
- idempotency;
- append-only versions.

The contract suite (`tests/core/storage/contract/`) runs every test once per adapter. The
General service adapter joins it when it exists.

**The test fake.** `tests/fakes/general_service.py` is the in-memory store plus test controls:

- a settable clock;
- one-line users and admins;
- a call log, to assert that the request id reaches the General service;
- failure injection for the readiness ping.

**Selection.** The backend is chosen by `STORAGE_BACKEND`:

- `fake` is the default and is for development.
- `local` survives restarts.
- `general` is the only backend production accepts.

`check_config` refuses `fake` and `local` in production. `general` is still a stub, so
`check_config` reports it, and `/readyz` answers 503 with `storage: unavailable`. That is
deliberate: an instance that cannot store data must not take traffic.

**Readiness.** `/readyz` pings the storage backend:

- `ok`: the backend answered.
- `skipped`: the fake has nothing to reach. This is neutral and does not affect readiness.
- `unreachable` or `unavailable`: 503.

The body never says which host or why. That stays in the logs.

### No writes outside the scratch root

The local adapter and the job workspaces write only under `SCRATCH_ROOT`. If it is unset, they
use `<system temp>/slideforge-scratch`. `paths.safe_join` builds every path and keeps every
write inside that root:

- Each part must be one plain segment: no separators, no `..`, no drive letters, no NUL, no
  leading dot and no Windows device names.
- The joined path is resolved, and it must still be inside the root. This also catches a
  symlink or junction planted inside the root.
- Record keys that are not plain segments are stored under their SHA-256, never as raw text.
- Temporary files are created next to their target, never in the system temp directory.

`tests/core/storage/test_local_fs.py` tests this. It drives every port and a workspace with
hostile names, then checks that every new file under the test directory is inside the root.

### Job workspaces

`job_workspace(blobs, ctx, root, job_id, inputs=...)` handles one job attempt in four steps:

1. Create `<root>/jobs/<job id>-<random>/`.
2. Fetch the named input blobs through the port, so the caller's ownership applies, into
   `in/`.
3. When the block ends cleanly, store every file under `out/` through the port. The results
   are in `ws.persisted`.
4. Remove the directory, however the block ends.

Two attempts of one job never share a directory.

### Jobs

```
app/core/jobs/
  queue.py     JobQueue: enqueue, lease, heartbeat, complete, fail
  limiter.py   InFlightLimiter: at most 3 expensive jobs per user
  worker.py    Worker: runs leased jobs with heartbeat and timeout
```

**Redis keys**, all under `sf:jobs:`:

- `ready` (ZSET): every unfinished job, scored by a Redis sequence. The oldest is tried first.
- `seq`: that sequence.
- `job:<id>` (HASH): type, owner subject, request id, attempts and the limiter slot.
- `lease:<id>`: the lease token, with a TTL.
- `inflight:<sha(subject)>` (ZSET): the limiter.

**Leases.** A worker takes a job with `SET lease:<id> <token> NX PX <lease>`. That one atomic
command is the lock. The TTL **is** the lease: heartbeats extend it.

A worker that is killed stops heartbeating. The key expires, and the job, which never left
`ready`, goes to the next worker that looks. No reaper is needed.

**Attempts.** Attempts are counted when a job is leased. Past `max_attempts`, the job is
finished as an error.

**Per-job timeout.** It is enforced twice:

- The worker cancels a handler that runs too long.
- A heartbeat past the deadline is refused, so a hung attempt loses its lease.

**Durable records.** Each record is written through the port at every step: queued, running
(with the attempt), progress, then done, error or queued again. The status routes read the
record, never Redis.

**Order of writes.** The durable record is written first, then Redis is cleaned up. If a
finished job comes back after a crash, `lease` sees the terminal record and drops it without
running it again.

**Idempotent enqueue.** A replayed `Idempotency-Key` returns the existing job. This happens
before an in-flight slot is taken, so a retry never gets a 429 for a job it already has, and a
429 never uses up the key. The same key with different input is a `Conflict`.

**The in-flight limit.** At most 3 expensive jobs per user (repo standards, "Rate limits"). The
limiter adds the job first, then counts, so two racing requests can both be refused but never
both admitted past the limit. Each slot carries its own expiry, so a slot whose job died frees
itself.

**The Phase 4 exit tests:**

- No writes outside the scratch root: `tests/core/storage/test_local_fs.py`.
- A killed worker's job is re-queued and finished by another worker:
  `tests/core/jobs/test_worker.py::test_a_killed_workers_job_is_requeued_and_completed_by_another_worker`.
- The contract suite is green for the fake and the local adapter.

### Not done yet

- **The real General service adapter.** It is blocked on D5.
- **A sweeper for stale `queued` records.** If the process dies between writing the durable
  record and adding the job to Redis, that record waits forever.
- **Wiring workers into the app process.** Starting worker loops in the lifespan, or as a
  separate entry point in the same image, comes when the first route enqueues a job (Phase 5
  and 7).
- **Two replicas on staging.** This Phase 4 exit item needs hosting (D2).
- **The local adapter is single-process.** It has one lock per process and lists by scanning
  directories. It is for development only.
