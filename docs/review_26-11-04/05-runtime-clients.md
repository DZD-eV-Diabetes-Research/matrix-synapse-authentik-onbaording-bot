# 05 Runtime robustness and HTTP clients

The client layer is small and consistent, sends reuse their transaction id across retries, and the event bus and sync
pump isolate handler failures. The weak spots are at the runtime seams. Two concurrent tasks (sync pump and reconcile
loop) can each create an onboarding room and overwrite each other's `m.direct` write (confirmed). Startup steps that
fail while Synapse or MAS are still booting are never retried. The sync pump never recovers from `M_UNKNOWN_POS`. And
nothing (healthcheck, process exit) notices a half-dead bot. Retry policy is uniform: it re-sends non-idempotent POSTs
and ignores `retry_after_ms`. The OAuth2 token provider has no single-flight. A bad config prints secrets to the log.

Experiments live in `/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/run/`
(`exp_http.py`, `exp_refresh_race.py`, `exp_mdirect_race.py`, `exp_httpx_log.py`, `bad_config*.yml`). Run them with the
project venv from the repo root.

| ID | Severity | Title |
|---|---|---|
| RUN-01 | high | Concurrent first welcomes for different users lose `m.direct` updates: duplicate onboarding rooms and duplicate welcomes |
| RUN-02 | high | Startup steps that fail during a cold start are never retried; the process runs degraded for its whole lifetime |
| RUN-03 | medium | Sync pump never recovers from `M_UNKNOWN_POS` (keeps resending the dead `pos` every 5 s forever) |
| RUN-04 | medium | No liveness signal: healthcheck probes only upstream APIs, and the run loops never exit on persistent failure |
| RUN-05 | medium | Non-idempotent POSTs (createRoom, media upload, kick) are blindly retried after ambiguous failures |
| RUN-06 | medium | OAuth2 token provider: no single-flight refresh, refresh-token race, MAS 5xx not retried, no 401 invalidation |
| RUN-07 | medium | A config validation error prints secrets (client secrets, token tails) to stderr / container logs |
| RUN-08 | medium | The reconcile pass is serialised behind onboarding through the awaited event bus |
| RUN-09 | medium | Shutdown is not bounded: no cancellation, 30 s long-poll, PID 1 ignores SIGTERM before handlers exist |
| RUN-10 | low | Retry and timeout policy: `retry_after_ms` ignored, 60 s timeout on every Matrix call, about 4 min worst case per call |
| RUN-11 | low | Every process and every healthcheck mints fresh MAS tokens; the `synapse_server.oauth2` path is untested end to end |
| RUN-12 | low | `build_app` leaks clients on startup failure and mixes fatal and best-effort steps arbitrarily |
| RUN-13 | low | One-shot commands run the full startup side effects and are not coordinated with a running service |
| RUN-14 | low | Offset pagination (Authentik page numbers, Synapse `from`) can skip an entry when the listing changes mid-read |
| RUN-15 | low | httpx logs one INFO line per HTTP request, including every sync long-poll |
| RUN-16 | low | Simplification: version negotiation is mostly dead, request paths are duplicated, a comment promises a fallback that does not exist |

## High

### RUN-01 Concurrent first welcomes for different users lose `m.direct` updates: duplicate onboarding rooms and duplicate welcomes

- **Severity:** high
- **Category:** bug
- **Location:** onbot/onboarding/welcome.py:131 (per-user lock), onbot/onboarding/welcome.py:192-204 (read-modify-write of `m.direct`), onbot/app.py:242 (two concurrent tasks), onbot/reconciler/engine.py:195, onbot/onboarding/listener.py:77-89
- **Status:** confirmed (experiment `exp_mdirect_race.py` with the unit-test fakes plus a yielding round trip)
- **Effort:** S

The welcome flow serialises per user (`self._locks[mxid]`), but `m.direct` is one shared account-data document that
`_ensure_direct_room` reads, appends to and writes back. Two different users' first welcomes running at once both
read the old document, and the second write drops the first user's room. The two concurrent callers exist by
construction: the reconcile task emits `user_synced` for every mapped user, while the sync pump task replays a join for
every member of every room (it always starts at `pos=None`, see `sync.py` docstring) and welcomes those too.

Scenario: first deployment against an existing user base (the legacy migration) or enabling welcome messages later.
The pump's initial sync and the first reconcile pass walk the same users in different orders and create DMs
concurrently. Experiment output: `DM rooms created: 2 | m.direct afterwards: {'@bob:...': ['!dm-2']}`. On the next
pass (or after a restart, since `_welcomed` is in memory) Alice gets a third room and the welcome message again:
`after next pass: DM rooms created: 3 | messages sent: 3`. The orphaned room still holds the force-joined user, is
invisible to `broadcast` (which reads `m.direct`), and shows up as a second "onboarding" room in the user's client.

Fix sketch: serialise `m.direct` mutation with one service-wide lock (or route all DM creation through a single
queue/worker), and re-read `m.direct` inside that lock right before writing. Overlaps the ONB area; listed here because
the race is created by the runtime composition, not by the welcome logic alone.

### RUN-02 Startup steps that fail during a cold start are never retried; the process runs degraded for its whole lifetime

- **Severity:** high
- **Category:** robustness
- **Location:** onbot/app.py:163-175, onbot/app.py:115-119, onbot/clients/matrix.py:130-149
- **Status:** confirmed (code reading); the MAS consequence (500 on send without a device row) needs live stack
- **Effort:** M

`build_app` performs five one-shot network steps and swallows each failure: version negotiation (app.py:163),
`ensure_device_registered` (matrix.py:148, catches everything), `_relax_bot_ratelimit` (app.py:75), the bot avatar
(avatar.py:81) and admin room provisioning (app.py:117, "continuing without it"). With the base client's retry policy
a refused connection fails all four attempts within about 3.5 s. Nothing ever retries these steps.

Scenario: host reboot, `docker compose up` starts Synapse, MAS and onbot together. Synapse needs 10 to 60 s. Onbot's
startup steps all fail in the first seconds, then the loops start and succeed once Synapse is up. Result for the rest
of the process lifetime: under MAS the bot's device is never registered, so per the docstring at matrix.py:120-128
every welcome `send` fails with a foreign-key 500, which is retried 4 times per message and then logged; the admin
control room does not exist and its `reconcile_completed` subscription was never made; broadcasts hit the rate limiter.
The healthcheck turns green as soon as the dependencies answer, so nobody is told.

Fix sketch: split startup into "required" steps (negotiation, device registration) that are retried with backoff until
they succeed or a deadline passes (then exit non-zero so Docker restarts the container), and "convergent" steps
(ratelimit override, avatar, admin room) that move into the reconcile pass or a `reconcile_completed` subscriber so they
heal on the next tick. At minimum, re-run `ensure_device_registered` when a send returns 500.

## Medium

### RUN-03 Sync pump never recovers from `M_UNKNOWN_POS`

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/sync.py:62-73, onbot/clients/matrix.py:483-485
- **Status:** confirmed (experiment `exp_http.py` [3]); how often Synapse issues `M_UNKNOWN_POS` needs live stack
- **Effort:** S

MSC4186 tells clients to drop `pos` and start a fresh connection when the server answers `400 M_UNKNOWN_POS` (expired
or unknown connection, for example after Synapse purges an idle connection or after a DB restore). `SyncPump.run`
treats every exception alike: log with traceback, sleep 5 s, retry with the same `self._pos`. Experiment: after one
good response the pump sent 43 further requests, all with `pos='p1'`, all answered 400. In production this means the
admin control room is deaf and join-driven onboarding is off until someone restarts the container, while the log gets
a full traceback every 5 s and the healthcheck stays green.

Fix sketch: on `ApiError` with `payload.errcode == "M_UNKNOWN_POS"` set `self._pos = None` and continue immediately
(the control room already guards against replay). Also use an exponential backoff with a cap instead of a fixed 5 s,
and log repeated identical failures without a traceback after the first.

### RUN-04 No liveness signal: healthcheck probes only upstream APIs, and the run loops never exit on persistent failure

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/healthcheck.py:77-135, Dockerfile:72-73, onbot/app.py:242, onbot/reconciler/engine.py:109-112, onbot/sync.py:69-72, onbot/discovery.py:110-113
- **Status:** confirmed (code reading)
- **Effort:** M

`onbot healthcheck` runs in a separate process and proves only that Synapse, Authentik and MAS answer with the
configured credentials. It cannot see the bot itself. All three loops catch `Exception` and keep going, and `gather`
therefore never sees a failure, so the process never exits on its own. States that look healthy today: the pump stuck
on `M_UNKNOWN_POS` (RUN-03); every reconcile pass failing (for example `ConfigurationError` because the space alias is
gone, engine.py:219); a pass blocked for hours behind onboarding (RUN-08); the degraded cold start (RUN-02). Conversely
the container is marked unhealthy when Authentik is down, although the bot is fine and will catch up. Note also that
plain Docker and Compose do not restart unhealthy containers, so the current check is informational at best.

Fix sketch: write a heartbeat file (for example `/tmp/onbot-heartbeat.json`) with `last_reconcile_ok_at`,
`last_sync_ok_at` and `last_discovery_ok_at`, updated by each loop on success. `onbot healthcheck` then reads the file
and fails when a timestamp is older than a threshold derived from the config (for example 3 x tick for reconcile,
5 min for sync), with no network calls and no token minting (RUN-11). Keep the dependency probe as a separate
`onbot check` command for operators. Optionally let the loops exit the process after N consecutive failures so a
`restart: unless-stopped` policy actually helps.

### RUN-05 Non-idempotent POSTs are blindly retried after ambiguous failures

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/clients/base.py:53-57, onbot/clients/base.py:114-123, onbot/clients/matrix.py:217, onbot/clients/matrix.py:243, onbot/clients/matrix.py:398, onbot/clients/matrix.py:311
- **Status:** confirmed (experiment `exp_http.py` [1], [1b]: the identical POST is re-sent); a duplicate room on a real Synapse needs live stack
- **Effort:** M

Every method is retried on 429, 5xx and any `httpx.TransportError`, including `ReadTimeout` and `RemoteProtocolError`,
where the server may already have executed the request. That is safe for PUTs with a fixed path (state events,
`send/.../{txn}`: the txn id is generated once outside the retry, experiment [2] shows one txn across all attempts),
but not for:

* `POST createRoom` without an alias, which is every onboarding DM (`create_direct_message_room`). A 502/504 from the
  reverse proxy or a 30 s read timeout after Synapse committed the room makes the retry create a second room with a
  second invite. The welcome flow records only the second one; the user sees an orphan invite.
* `POST createRoom` with an alias: the retry fails with 400 `M_ROOM_IN_USE`, which is raised, so the follow-up
  `m.space.child` link (matrix.py:223) is skipped for that pass.
* `POST media/v3/upload`: duplicate uploads (orphan media, harmless but wasteful).
* `POST kick` / `invite`: the retry gets a 403 and turns a success into an error.

Fix sketch: add an `idempotent: bool` flag to `request_json` (default true for GET/PUT/DELETE, false for POST), and for
non-idempotent calls retry only on errors that prove the request was not processed (`ConnectError`, `ConnectTimeout`,
`PoolTimeout`, 429, 503 with `Retry-After`). For aliasless `createRoom`, tag the room with a creation nonce in
`initial_state` or `creation_content` so the caller can find and adopt an already-created room instead of creating
another.

### RUN-06 OAuth2 token provider: no single-flight refresh, refresh-token race, MAS 5xx not retried, no 401 invalidation

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/auth/token_provider.py:91-121 (`get_token`, `_refresh`, `_token_request`), onbot/auth/token_provider.py:118, onbot/clients/base.py:110-112
- **Status:** confirmed (experiments `exp_http.py` [4], [5] and `exp_refresh_race.py`)
- **Effort:** S

`get_token` has no lock. When the token is missing or inside the 30 s margin, every concurrent caller performs its own
exchange. The CS token provider is shared by the reconcile task, the sync pump, the control room and broadcast fan-out
(concurrency 5). Findings:

1. 10 concurrent `get_token()` calls produced 10 `client_credentials` exchanges and 10 distinct tokens (experiment [4]).
   Each exchange is a new MAS OAuth2 session (see RUN-11).
2. With a single-use refresh token (MAS rotates them), 5 concurrent callers after expiry: 4 raised `OAuth2TokenError`,
   and the provider ended with `_refresh_token = None` because the losers' error path
   overwrote the winner's fresh refresh token (token_provider.py:118; `exp_refresh_race.py`).
3. A MAS 503 on the token endpoint raises `OAuth2TokenError`, which `_is_retryable_exc` does not recognise, so the API
   call fails on the first attempt (experiment [5]: 1 token attempt, 0 API attempts), whereas a transport error to MAS
   is retried. Inconsistent, and a MAS restart therefore fails whole reconcile passes.
4. A 401 from Synapse never invalidates the cached token; a revoked token is reused until its local expiry.

Fix sketch: guard `_refresh` with an `asyncio.Lock` and re-check validity after acquiring it; only clear the refresh
token on `400 invalid_grant`; make `OAuth2TokenError` retryable for 5xx; on a 401 `M_UNKNOWN_TOKEN` call a
`provider.invalidate()` and retry once.

### RUN-07 A config validation error prints secrets to stderr / container logs

- **Severity:** medium
- **Category:** security
- **Location:** onbot/cli.py:73 (`config = load_config()` unguarded), onbot/config.py:1367-1373
- **Status:** confirmed (ran `onbot run` and `onbot reconcile-once` against fake configs `bad_config.yml`, `bad_config2.yml`)
- **Effort:** S

`load_config()` raises a pydantic `ValidationError` that escapes `main()` as a raw traceback. For a missing field in a
nested model pydantic prints the whole parent dict as `input_value`. With `mas_admin.client_id` missing, the output
was `input_value={'url': 'https://mas.exam...: 'MAS_ADMIN_SECRET_x9'}`: the complete MAS admin client secret (the
credential that can lock and deactivate any account). With `bot_user_id` missing, the tail of the bot access token was
printed (`...ERSECRET_BOT_TOKEN_123'`). Pydantic truncates long reprs in the middle, so whether a secret appears in full
depends on its position and length. Container logs are usually shipped to a log aggregator. Exit code is 1, the same
as any runtime crash.

Fix sketch: catch `ValidationError` in `main()`, print `exc.errors(include_input=False, include_url=False)` as one line
per field (`synapse_server.bot_user_id: Field required`) and return exit code 2. Mark secret fields as `SecretStr` so
they never repr in clear text. Overlaps SEC; listed here because it is a CLI error-handling defect.

### RUN-08 The reconcile pass is serialised behind onboarding through the awaited event bus

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/reconciler/engine.py:195, onbot/events.py:46-54, onbot/onboarding/listener.py:83-95
- **Status:** confirmed (code reading)
- **Effort:** M

`_gather_mapped_users` does `await self.events.emit(Signal.user_synced, mxid=mxid)` inside the loop over users, and
`emit` awaits all handlers. So the gather phase runs the full welcome flow for every not-yet-welcomed user, one after
another, before any room, membership or power-level convergence happens. The bus gives isolation from exceptions but no
decoupling in time and no backpressure policy.

Scenarios: (a) fresh deployment with 500 users: createRoom, force-join, state writes and sends for each user run
serially before the first room is touched; group rooms appear many minutes late. (b) Any persistent send failure (for
example the MAS 500 from RUN-02): each message burns 4 attempts and about 3.5 s, failed users are not remembered, so
every pass repeats it for every affected user. 200 users add more than 10 minutes to every pass, every tick. (c) After
each restart `_welcomed` is empty, so each pass starts with 3 CS reads per user before converging anything.

Fix sketch: have the bus enqueue onto an `asyncio.Queue` consumed by a dedicated onboarding worker task (bounded queue,
deduplicate by mxid), or emit `user_synced` after convergence and do not await it. A worker also gives one place to
serialise `m.direct` (RUN-01).

### RUN-09 Shutdown is not bounded

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/reconciler/engine.py:98-129, onbot/app.py:231-242, onbot/sync.py:55-75, Dockerfile:75
- **Status:** confirmed for the pump (experiment `exp_http.py` [6]: `pump.run` returned 2.8 s after `request_stop` with a 3 s long-poll); PID 1 behaviour and the real Docker timing need live stack
- **Effort:** S

Stopping is purely cooperative. `request_stop()` sets flags, but nothing cancels in-flight work:

* the sync pump finishes its long-poll (`timeout_ms=30000`, plus up to 4 retries) before checking the flag;
* the engine finishes the current reconcile pass, which can take minutes at scale;
* `docker stop` waits 10 s by default, then sends SIGKILL.

So a normal `docker stop` or redeploy usually ends in SIGKILL (exit 137), and it can land between two dependent writes,
for example after `createRoom` but before `set_account_data(m.direct)` in `welcome.py:197-204` (orphan DM room, a second
one next start) or between `createRoom` and the `m.space.child` write.

In addition, signal handlers are only installed inside `engine.run()` (engine.py:104), after `build_app`'s startup
steps. The image runs `onbot` as PID 1 (`ENTRYPOINT ["onbot"]`, no init). Per pid_namespaces(7), PID 1 only receives
signals it has a handler for, so SIGTERM is ignored during startup and for the whole lifetime of `reconcile-once` and
`broadcast` containers; those always wait for the SIGKILL.

Fix sketch: run the three loops as tasks in an `asyncio.TaskGroup` (or keep references) and on SIGTERM cancel the pump
and discovery tasks immediately and give the engine a bounded grace period (for example 8 s) before cancelling it.
Install the signal handlers at the start of `run_service`/`main`, not in `engine.run`. Add `init: true` or `tini` to
the image and document `stop_grace_period` in deployment.md.

## Low

### RUN-10 Retry and timeout policy: `retry_after_ms` ignored, 60 s timeout on every Matrix call

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/clients/base.py:29, onbot/clients/base.py:114-123, onbot/clients/matrix.py:87-88
- **Status:** confirmed (experiment `exp_http.py` [2]: a 429 asking for 20 s gave up after 4 attempts in 3.5 s)
- **Effort:** S

The retry is a blind `wait_exponential(0.5, max=10)` for 4 attempts. 429 responses carry `retry_after_ms` (Matrix) or
`Retry-After` (Authentik, proxies); both are ignored, so a sustained throttle always turns into an error after 3.5 s
(the broadcast docstring admits this). The Matrix client raises the timeout to 60 s for all calls because sliding sync
long-polls, so against a hung Synapse every ordinary call can take 4 x 60 s + 3.5 s, about 4 minutes, and a reconcile
pass of a few hundred calls effectively never ends. The default httpx pool limits (100 connections) are fine for the
current concurrency.

Fix sketch: a custom tenacity `wait` that uses `retry_after_ms` / `Retry-After` when present (capped); keep 30 s as the
default timeout and pass `timeout=timeout_ms/1000 + 15` per request only for `sliding_sync`.

### RUN-11 Every process and every healthcheck mints fresh MAS tokens; the `synapse_server.oauth2` path is untested end to end

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/healthcheck.py:79-102, onbot/app.py:49-61, onbot/app.py:182-190, onbot/auth/token_provider.py:239-249
- **Status:** needs live stack
- **Effort:** S

The Docker healthcheck starts a new process every 60 s, which builds new providers and (with `mas_admin` set, or with
`synapse_server.oauth2`) performs a fresh `client_credentials` exchange each time, two for the CS provider because the
matrix and admin probes run concurrently (RUN-06). Without a refresh token the main process also does a fresh exchange
every expiry. If MAS keeps one OAuth2 session per exchange until a cleanup job removes it, this adds thousands of
sessions per day. Separately, `synapse_server.oauth2` (bot acts on the CS API with a `client_credentials` token) is not
covered by the integration tests (only the MAS admin client is); a client-credentials token has no user, and it is not
clear that Synapse under MSC3861 accepts it for `/whoami`, `createRoom` or `send`. Live test: count rows in MAS
`oauth2_sessions` before and after 10 minutes of healthchecks, and run a reconcile pass with `synapse_server.oauth2`.
If the CS path does not work, remove it from the config model rather than documenting it as "forward-looking".

### RUN-12 `build_app` leaks clients on startup failure and mixes fatal and best-effort steps arbitrarily

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/app.py:144-228, onbot/app.py:125-126
- **Status:** confirmed (code reading)
- **Effort:** S

Five clients and two token providers are constructed before the `try/finally` at app.py:219. Any exception before that
(for example `build_matrix_token_provider` raising after the Authentik client exists, or `handler.start()` at
app.py:126, which sits outside the `try` in `_build_control_room` and does a Synapse read) skips every `aclose()`.
Process exit makes this mostly cosmetic, but it also shows that "fatal vs best-effort" was decided per call site:
provisioning the admin room is best-effort while reading its cursor right afterwards is fatal. Fix sketch: build
everything inside an `AsyncExitStack` (`await stack.enter_async_context(client)` right after construction) and decide
fatality per step class as proposed in RUN-02.

### RUN-13 One-shot commands run the full startup side effects and are not coordinated with a running service

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/app.py:245-264, onbot/cli.py:85-92, docs/deployment.md (CLI commands section)
- **Status:** plausible
- **Effort:** S

`broadcast` and `reconcile-once` go through `build_app`, so each run registers the device, overrides the rate limit,
fetches and compares the avatar, provisions the admin room and re-invites admins. That is slow and surprising for
"send one notice". More importantly the docs suggest running `reconcile-once` next to the service, and the welcome flow
is serialised only in process memory (`WelcomeService._locks`). Two processes welcoming the same new user at the same
time both see an empty `m.direct` entry and both create a DM room. Fix sketch: give one-shot commands a lean builder
(clients only), and document that `reconcile-once` must not run against a live service, or route on-demand reconciles
through the running service (the admin room already has a trigger path via `engine.trigger`).

### RUN-14 Offset pagination can skip an entry when the listing changes mid-read

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/clients/authentik.py:23-29, onbot/clients/synapse_admin.py:29-38, onbot/clients/base.py:199-220
- **Status:** plausible
- **Effort:** M

Authentik pages by page number and Synapse `v2/users` / `v1/rooms` by numeric offset. If an entry before the current
offset disappears between two page fetches (a user is deactivated in Authentik while the bot lists `is_active=true`
users, or a Matrix account is deactivated while listing `v2/users`, which hides deactivated accounts), every later
entry shifts up by one and the first entry of the next page is never seen. With
`kick_matrix_room_members_not_in_mapped_authentik_group_anymore` enabled, a skipped active user is not in the desired
set and is kicked from their group rooms for one pass, then re-joined on the next. Only matters above one page (100
users). Fix sketch: after a multi-page listing, compare `pagination.count` (Authentik) or `total` (Synapse) with the
number of items collected and re-read once on mismatch; or treat a user who disappears for exactly one pass as "unknown"
rather than "remove".

### RUN-15 httpx logs one INFO line per HTTP request

- **Severity:** low
- **Category:** maintainability
- **Location:** onbot/logging.py:15-26
- **Status:** confirmed (experiment `exp_httpx_log.py` printed `INFO httpx: HTTP Request: POST .../sync?timeout=30000&pos=0 "HTTP/1.1 200 OK"`)
- **Effort:** S

`configure_logging` sets the root logger to INFO, and httpx logs every request at INFO. An idle bot writes two lines per
minute for the sync long-poll alone; a reconcile pass writes one line per room state read, which drowns the bot's own
messages. Fix sketch: `logging.getLogger("httpx").setLevel(logging.WARNING)` (and `httpcore`) unless the configured
level is DEBUG.

### RUN-16 Simplification: dead version gating, duplicated request paths, a misleading comment

- **Severity:** low
- **Category:** simplification
- **Location:** onbot/clients/versions.py:26, onbot/clients/versions.py:59-61, onbot/clients/matrix.py:110-113, onbot/app.py:161-166, onbot/clients/base.py:125-185
- **Status:** confirmed (code reading, grep for consumers)
- **Effort:** S

* `supports_authenticated_media()` is only used in a log line; downloads always use the authenticated v1 endpoint.
  `ApiClientMatrix.versions` has no consumer. `AUTHENTICATED_MEDIA_SINCE` and `_version_tuple` exist for the log line.
* The comment at app.py:161-162 says "the listener re-checks and falls back if needed". Nothing re-checks: when
  negotiation fails, `_versions` stays `None` and `sliding_sync` simply skips the support check.
* `request_json` and `request_raw` duplicate URL building, param cleaning, auth, error mapping and retry; one private
  `_send(method, path, *, params, json=None, content=None, headers=None)` returning the `httpx.Response`, with thin
  JSON/bytes wrappers, removes about 30 lines and gives a single place for the RUN-05/RUN-10 policy changes.

Fix sketch: drop the authenticated-media check and the unused property (keep the sliding sync gate), fix the comment,
and merge the two request paths.

## Checked and fine

* `send_text_message` generates its txn id outside the retry, so retried sends are deduplicated by Synapse.
* No fire-and-forget `create_task`; nothing can be garbage collected mid-flight.
* `EventBus.emit` and `SyncPump._dispatch` isolate handler exceptions; one failing subscriber does not stop others.
* The engine clears its trigger before a pass, so a discovery trigger raised during a pass is not lost.
* Memory: per-user sets and locks are bounded by the user count; the control room's `_seen` is a bounded deque.
