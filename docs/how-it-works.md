# How it works

A plain-English tour, with no jargon assumed. Start here.

## 1. What this service is for

auxi has a web app called **Darwin** that builds slide decks. Today Darwin's backend is a set of
small functions on Netlify, and the work of turning a slide design into a real PowerPoint file
happens in a separate app called **Slide Studio**. This service brings those together into one
place, written in Python and run on Azure:

- it will **design** slides (an AI model writes each slide as a web page, checks it, and fixes it);
- it will **export** them to PowerPoint, by rendering each page in a headless browser and
  rebuilding it as native PowerPoint shapes;
- it will **serve Darwin's API**: every address Darwin's web app and the auxi Connector call
  today (`/api/decks`, `/api/status`, and so on) keeps working exactly as it does now, just
  answered by this service instead of Netlify.

None of that is here yet. This first phase builds the frame everything else hangs on.

## 2. What is here today

The service starts, answers two health questions, and does the plumbing every later feature
relies on.

**Two health questions.** A hosting platform needs to know whether to send traffic to an
instance, and whether to restart it.

- `GET /healthz` answers "is the process alive?" It checks nothing else, so a failure really
  means "restart me". It also says **which commit is running**: when the deploy package is
  built, the commit's id is written into it, and `/healthz` reads it back. So "what is
  deployed?" is one request, not an investigation.
- `GET /readyz` answers "can this instance do useful work?" It checks that Redis answers and,
  in production, that the configuration is valid. If not, it answers 503 and the platform
  keeps traffic away. It never says *why* in its answer (which host, which setting); the why is
  in the logs.

**A request id on every request.** The gateway (the single front door the platform team is
building) will stamp every request with an id. The service reads it, or makes one up if it is
missing, writes it on every log line about that request, and sends it back in the response. If
a user reports an error, that id finds every line about it.

**Errors in two shapes.** When something goes wrong, the caller gets a short, safe message,
never a stack trace or an internal detail.

- Darwin's existing routes answer errors the way Darwin always has: `{"error": "message"}`.
  Changing that would break the web app and the Connector, which read that shape.
- Every new route answers in the standard shape, *Problem Details*: a type, a title, the status,
  a human message and the request id, plus a list of which fields were wrong when the input was
  invalid.

A route says which shape it uses when it is defined, and the tests check that every route has
made that choice.

**Logs and telemetry.** Every log line goes to standard output with the instance, request and
job ids, and anything that looks like a credential is blanked out before it is written. When
the Application Insights connection string is set, request timings and errors also go there,
per route and tagged by feature; when it is not set (on a laptop, in CI), telemetry quietly
does nothing.

**A startup config check.** On startup the service checks its own settings and logs anything
wrong. In production a bad configuration also makes `/readyz` fail, so a misconfigured instance
never takes traffic.

**Redis.** A small, fast store that will hold the job queue, the "how many jobs is this user
running" counters and the rate limits. It holds no lasting data: decks, brands and files will
live in the **General service** (auxi's .NET service in front of SQL Server and blob storage).

## 3. What happens to a request

```
caller ─► request id (read or made) ─► CORS check ─► route ─► response (+ request id)
                                                      │
                                                      └─ error? ─► {"error"} or Problem Details
```

1. The request-id step reads the gateway's id or makes one, and binds it for logging.
2. CORS lets Darwin's web page, and only that page, call the service from a browser. (Once the
   gateway exists, browsers will go through it.)
3. The matching route runs.
4. If it fails, the error handler picks the route's error shape and answers safely.
5. The response leaves with the request id, and one access line plus one timing metric is
   recorded.

## 4. How it is kept honest

- **The route table.** Every route has a row saying who may call it, what protects it, and how
  hard it is rate limited. [route-controls.md](route-controls.md) is generated from that table,
  and the tests fail if a route has no row or a row's promise is not kept by the code.
- **The gate.** `scripts/gate.py` runs locally exactly what CI runs: the tests, lint, type
  checks, the dependency audit, the secret scan and the security scan.
- **The deploy package** is built by CI from the exact commit it tested, and carries that
  commit's id, which `/healthz` reports.

## 5. What comes next

The migration plan's phases, in short: the slide engine (Phase 2), the AI design features
(Phase 3), storage through the General service and background jobs (Phase 4), sign-in and
per-route protection (Phase 5), then Darwin's routes and data moving over (Phase 7). Where it
will be hosted is still open (decision D2), most likely as a container, because the engine
needs a headless browser.
