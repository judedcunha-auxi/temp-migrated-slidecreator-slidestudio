# Docs

Start with [how-it-works.md](how-it-works.md): it explains the service in plain English and makes
everything else readable. Then pick by what you are trying to do.

## I want to understand it

| Doc | |
|---|---|
| [how-it-works.md](how-it-works.md) | **Start here.** What the service is for, what happens to a request, and what is still to come. |
| [architecture.md](architecture.md) | The design, one section per area: the export engine, storage through the General service port, and the Redis job queue. |
| [licensing.md](licensing.md) | Third-party code that is licence-gated (StageFlow, decision D3), and what is kept out on purpose. |
| [route-controls.md](route-controls.md) | Every route, who may call it and what protects it. Generated from a table the tests hold to the code. |
| [general-service-requirements.md](general-service-requirements.md) | What this service needs from the General service: every operation, the access rules, idempotency and request ids. The hand-over spec for the .NET team. |

## I want to change it

| Doc | |
|---|---|
| [development.md](development.md) | Running it locally, **which interpreter to use**, the tests, and how to add a route or a setting. |
| [route-controls.md](route-controls.md) | Read before adding a route: the row you add there is part of the change. |

## I want to ship or run it

| Doc | |
|---|---|
| [deployment.md](deployment.md) | Branches and release flow, the deploy artifact, verifying a deploy. Hosting is not decided yet (D2). |
| [../scripts/README.md](../scripts/README.md) | The gate, packaging, `verify_deploy.py` and `whats_deployed.py`. |

## Keeping these honest

A doc that describes code that has changed is worse than no doc. When a change makes a doc
wrong, fix it in the same commit. Where a contract between a document and the code can be a
test, it is one:

- `tests/test_docs_links.py` checks every link and anchor, and that the required documents exist.
- `tests/test_route_controls.py` regenerates and checks the table in `route-controls.md`.
- `tests/config/test_env_example.py` checks that `.env.example` lists every variable the code reads.
- `tests/test_ci_workflow.py` checks the CI workflow keeps the standard job and step names.
- `tests/core/storage/test_fake_and_stub.py` checks that `general-service-requirements.md` covers
  every storage port operation.

Still to write, when there is something to say: `testing.md`, `security.md` and
`observability-runbook.md` (once there is traffic to ask questions about).
