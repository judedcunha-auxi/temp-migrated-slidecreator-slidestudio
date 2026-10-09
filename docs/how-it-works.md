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

The frame (Phase 1), the export engine (Phase 2) and the AI design core (Phase 3) are here, and
the first of Darwin's routes have moved in (Phase 7a): the storyline, the intake chat, the
PowerPoint exports, brand setup, the admin pages, usage events and the sign-in helper.

## 2. What is here today

The service starts, answers two health questions, and does the plumbing every later feature
relies on.

**Two health questions.** A hosting platform needs to know whether to send traffic to an
instance, and whether to restart it.

- `GET /healthz` answers "is the process alive?" It checks nothing else, so a failure really
  means "restart me". It also says **which commit is running**: when the deploy package is
  built, the commit's id is written into it, and `/healthz` reads it back. So "what is
  deployed?" is one request, not an investigation.
- `GET /readyz` answers "can this instance do useful work?" It checks that Redis answers, that
  the storage backend answers, and, in production, that the configuration is valid. If not, it answers 503 and the platform
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

**The export engine** (`app/engine/`, Phase 2). It turns slide designs into real, editable
PowerPoint on the customer's own template. No route calls it yet (Phase 3 wires it in), but it is
complete and tested:

1. **Read the template.** An uploaded `.pptx` master is read into a *manifest*: the slide size,
   the colours and fonts, every layout and every placeholder box (where the title goes, where the
   body goes). A picture of each empty layout comes from **PptxRender**, a separate rendering
   service the engine calls over the network.
2. **Measure the slide.** Each slide is a small web page. The engine opens it in a hidden
   browser (Chromium), sized exactly like the slide, in the template's fonts, and writes down
   where every piece of text, every line break, box, colour, image and drawing ended up.
3. **Understand it.** Hand-drawn bar and doughnut charts that show their numbers become real
   charts; a heading that sits where the template's title goes becomes the slide's title.
4. **Rebuild it in PowerPoint.** On a copy of the template, each slide is rebuilt from native
   shapes, text boxes, tables, pictures and charts with their own data, so the result can be
   edited in PowerPoint like any deck. The template's own parts are left byte-for-byte as they
   were.
5. **Check it** (in testing): text that would overflow in PowerPoint, overlaps, what had to
   become a picture, and, with a full PptxRender build, a pixel comparison against the browser.

The same slides always give exactly the same file, byte for byte. Each export works in its own
scratch folder, and a small pool of browsers lets several exports run at once.

Two things to know: the server has no Microsoft fonts, so it measures Calibri in **Carlito** and
Arial in **Liberation Sans**, look-alike fonts with identical letter widths, and still names the
original font in the file. And two of the engine's files are derived from third-party code whose
licence is not yet granted ([licensing.md](licensing.md)); they must not ship to production until
that is settled. [architecture.md](architecture.md) has the details.

**The AI design core** (Phase 3). This is where slides get designed. A slide starts either as a
line in the deck's plan (its conclusion, its kind of chart, its bullets) or as a finished picture,
and ends as an editable PowerPoint slide on the customer's own template:

1. **The template.** The brand's look (colours, fonts, logo, header band) was read from the
   customer's PowerPoint once, at brand setup. For each slide it is rebuilt as a real PowerPoint
   template. If the person turned branding off, the template keeps only where the title goes.
2. **The design.** An AI model (Claude) writes the slide as a small web page, using a set of tools:
   save the slide, edit part of it, look at a picture of it, plan the main exhibit, look up layout
   recipes and ready-made exhibit snippets. After saving, the code checks the slide (will PowerPoint
   be able to rebuild it? is anything overlapping, too small, off the grid?) and tells the model.
3. **A second pair of eyes.** When the model looks at its slide, a **separate** AI call, which has
   not seen any of the reasoning, judges the picture against a strict checklist and lists at most
   five fixes. The model fixes them and looks again. This happens at most twice or three times a
   slide, so it cannot loop.
4. **The brand's safe area.** Many templates reserve a header band and a footer. If the slide's
   content strays outside the brand's safe area (the "workzone"), the slide gets one more round to
   move it back.
5. **The PowerPoint.** The export engine above rebuilds it. The result says how many shapes were
   built, how many things a person may want to look at, and what the AI calls cost.

A whole deck can be designed several slides at a time, with a limit on how many at once, and
finished slides are put together into one deck. Every AI call's cost is worked out from the tokens
it used and added up per slide ([models-and-cost.md](models-and-cost.md)). If an answer is cut off
because it was too long, the model is asked to carry on rather than the slide failing. No test ever
calls a paid AI service: the tests use a scripted stand-in.

**Storage, through one door.** Everything that must last goes through a single interface to the
General service: users, brands, decks, slides and every version of them, files, job records,
the cost ledger. The General service is not built yet, so for now the service runs against a
stand-in that keeps data in memory, or in files on a developer's machine. A shared set of tests
checks that the stand-ins behave as the real thing must: you only see your own decks, members of
an organisation can use its brand but not change it, a retried request does not do the work
twice, and a slide's old versions are never lost. The list of what the General service must do
is in [general-service-requirements.md](general-service-requirements.md).

**Background jobs.** Slow work (designing slides, exporting a deck) runs as a job. A job waits
in a Redis queue; a worker takes it and holds it with a lease it keeps renewing. If the worker
dies, the lease runs out and another worker picks the job up. Nobody runs more than three
expensive jobs at once. What the user sees (queued, running, done) is kept with the General
service, not in Redis. [architecture.md](architecture.md) has the design.

**The storyline** (`app/core/storyline/`, Phase 3). Before any slide is designed, a deck needs a
plan: a title, and for each slide a one-sentence conclusion, the kind of slide it is, the picture
that proves it (a "framework" such as a 2x2 matrix or a waterfall chart), three or four bullets of
evidence, and which section of the deck it belongs to. Darwin and Slide Studio each had their own
version of this step; this is the two merged into one.

1. **The intake chat.** A short interview: one question per reply, until the topic, audience, key
   messages and slide count are pinned down. Each reply costs exactly one AI call (Darwin's version
   could make two: a bug, C14).
2. **The draft.** One AI call writes the whole plan as structured data. Two styles: *standard*
   (Darwin's: 3-4 short bullets per slide) and *dense* (Slide Studio's: up to 6 bullets, for slides
   that combine several exhibits). The plan can be a full deck, a set of slides, or a single slide.
3. **The check and the repair.** The answer is checked against the rules (a standard slide with
   five bullets is rejected, as Darwin rejects it). Then anything fixable is fixed and reported,
   never failed: a "spider chart", which PowerPoint cannot build natively, becomes a scoring
   matrix; a layout reference that does not exist is dropped; section-divider slides are added
   between sections.

Arabic decks get the same plan written in Arabic, with Western digits, and words like "left rail"
mirrored in anything the AI reads about layout. The draft runs as a background job; its result is
exactly what Darwin's web app reads today. The storyline asks for "a model that answers in
structured data"; in the service that is Claude, through the same model layer the design loop uses,
and in the tests a scripted stand-in, so they cost nothing. [architecture.md](architecture.md#the-storyline-phase-3) has the design.

**Darwin's first routes.** Darwin's web app and the auxi Connector call addresses like
`/api/storyline` and `/api/pptx-submit`. Twenty-four of the thirty-seven are answered here now, exactly as
Netlify answers them today, down to the odd corners (an unknown job id says "pending" forever; a
broken request body gets "Internal error", not "Bad request"), because the callers depend on them.
The plan's recorded contract for each route is the test: every documented answer is reproduced, or
the test says why it can no longer happen. Behind the routes, the old background functions are
jobs on the queue, and "send it to Slide Studio" is a call to the design pipeline inside this
service. [darwin-api.md](darwin-api.md) explains the layer and how to move the remaining routes.

**Brands.** A brand is a company's look: colours, fonts, logo, slide masters and the "furniture"
(logo, footer, header band) lifted from its PowerPoint template. A brand is either personal or an
organisation's: an admin creates it and maps an email domain to it, and everyone who signs in with a
confirmed address at that domain can use it but not change it. Uploading a template reads it inside
the service (colours, fonts, every layout's furniture) and draws a picture of each layout through the
PowerPoint renderer, so the person can pick which layout is the cover, the section divider and the
content slide. A brand-guidelines PDF is read by the AI for colours, fonts and a style note, and a
preview renders one sample slide in the brand's look (cached until the brand changes). Every one of
these runs as a background job in the name of the person who asked, so the service checks again,
when the job runs, that they may still touch that brand.

**Who is calling.** Darwin's routes need a signed-in user. The caller sends a token from the
identity provider; the service checks its signature against the provider's published keys, that
it has not expired, and that it was issued for this service, then finds (or creates) the user's
profile through the storage door. Which identity provider is still to be decided, so the service
names it by configuration; the tests use a pretend provider with its own keys. There is no
back-door developer key.

## 3. What happens to a request

```
caller ─► request id (read or made) ─► CORS check ─► route ─► response (+ request id)
                                                      │  └─ token check, then the work
                                                      │     (or a job on the queue)
                                                      └─ error? ─► {"error"} or Problem Details
```

1. The request-id step reads the gateway's id or makes one, and binds it for logging.
2. CORS lets Darwin's web page, and only that page, call the service from a browser. (Once the
   gateway exists, browsers will go through it.)
3. The matching route runs. A Darwin route checks the caller's token first (or, on a few routes,
   the method first, because that is the order Darwin used), then does the work or queues a job
   and answers with its id.
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

The migration plan's phases, in short: the AI design features (Phase 3, here; paid runs on staging
still to be approved), storage through the General service and background jobs (Phase 4), sign-in and per-route
protection (Phase 5: rate limits, in-flight caps, licensing; the route table marks what each
route still lacks as TODO-P5), then the rest of Darwin's routes and its data moving over (Phase 7). It will run as a
container on Azure App Service (decision D2), because the engine needs a headless browser and
fonts.
