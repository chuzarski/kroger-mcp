# Hosted Kroger MCP: library upgrade, shortlist bulk search, in-server token refresh

Status: draft for approval. Date: 2026-11-24. Branch: `hosted-mcp` (fork of CupOfOwls/kroger-mcp).

## Intent and why

The maintainer will run this server as a long-lived hosted service, reachable by AI agents over
Streamable HTTP. Three problems stand in the way:

1. The MCP libraries are behind (fastmcp 3.4.x on MCP SDK v1; current is fastmcp 4.x on SDK v2).
2. `bulk_search_products` returns one full product record per hit (nutrition, allergens, images,
   pass-through fields). A 25-term call has been seen to produce hundreds of thousands of characters,
   which swamps an agent's context. Agents then truncate it, avoid the tool, or lose data silently.
3. The user token is renewed only when a cart or profile call finds it expired. A hosted server
   that sits idle for days leaves an agent facing "sign-in needed" with no warning. An external cron
   job used to paper over this; it should not be needed.

## Goals

- Run on current fastmcp 4.x / MCP SDK v2, with accurate tool annotations (read-only vs. cart-changing).
- `bulk_search_products` answers in a size an agent can read: a shortlist by default.
- The server keeps the Kroger sign-in alive by itself and reports honestly when it cannot.
- A container image the operator can run behind a reverse proxy (packaging only; the proxy is out of scope).

## Non-goals

- Reverse proxy, gateway, Cloudflare, TLS, client authentication, secrets handling. The operator owns these.
- Persisting a half-finished sign-in across a restart. Starting the sign-in over is acceptable (decided).
- A checkout tool, or reading the real Kroger cart (the API does not allow it).
- Staying mergeable with upstream. Changing `bulk_search_products` in place is accepted drift.

## Anti-goals (a failure even if it "works")

- A new bulk tool next to the old one, so an agent can still call the context-flooding one.
- A refresher that races other writers of the token file, or that burns a refresh token Kroger already rotated.
- Silent loss: a failed search reported as "no results", or a dead sign-in reported as healthy.
- Private operator details (store, zip, credentials, hostnames) in this public repository.

## Constraints

- FastMCP 4.2 needs Python 3.11+. The package moves to `requires-python >=3.11`, and the CI matrix drops 3.10.
- Tools are synchronous and run in worker threads; logging to the client uses `anyio.from_thread`.
- Token and state files live in `KROGER_TOKEN_DIR` and need a persistent volume.
- The Kroger API cannot show or empty the real cart; view/remove/clear change only the local record.
- Access tokens last 1800 s. Whether the refresh token rotates on each refresh, and its lifetime, are unverified.

## Approaches considered

**Bulk search shape** (decided):
- One auto-picked product per term: smallest output, but the agent cannot see brand/price differences and
  the cart cannot be corrected later. Rejected.
- Mixed (auto-pick when obvious, shortlist otherwise): two behaviors to explain and test. Rejected.
- **Shortlist, up to 3 one-line options per term. Chosen.** The agent decides before adding, which matters
  because added items cannot be removed from the real cart.

**Where it lives** (decided):
- A second compact tool: an agent could still pick the raw one. Rejected.
- **Change `bulk_search_products` itself**, shortlist by default, `detail: "full"` for at most a few terms. Chosen.

**Token refresh** (decided):
- External timer or sidecar process, or a second process writing the token: two writers to one file. Rejected.
- **Background task inside the server**, using kroger-api's token-file lock. Chosen.

## Design

### 1. Library upgrade

- Dependencies: `fastmcp>=4.2.5,<5` (verify the exact floor when implementing), `requires-python>=3.11`,
  drop 3.10 from classifiers and CI. Refresh `uv.lock`. Keep `kroger-api>=0.4.0`.
- Work through the fastmcp 3 -> 4 upgrade guides. Known touch points: `errors.py` (`mcp.types`,
  `ToolResult(is_error=...)`, middleware, `ValidationError`), `shared.py` `log_to_client` (the
  `ctx.info` bridge emits an SDK deprecation warning in 4), `Image` import in `product_tools.py`.
  Audit camelCase MCP field uses. The existing test suite is the safety net: it must pass first, unchanged
  in intent, before any feature work.
- Annotate every tool: `readOnlyHint=True` for searches, lookups and info; `destructiveHint=True` for
  tools that change the local cart record or sign-in (clear/remove/sign out); non-destructive writes
  (add to cart, set preferences) are not read-only and not destructive. Add a test that every registered
  tool has explicit annotations, so a new tool cannot forget.
- Re-test the HTTP transport on fastmcp 4: Host/Origin handling behind a proxy (it accepted any on 3.x).
  Required outcome: the default image must answer requests that arrive through a reverse proxy with a
  non-localhost Host header. If fastmcp 4 / SDK v2 enables DNS-rebinding protection by default, add one
  optional setting, `KROGER_MCP_ALLOWED_HOSTS` (comma-separated Host values; `*` disables the check), and
  document it. If the check is off by default, add nothing. (Builder-added env var is allowed for this.)

### 2. Shortlist bulk search

Signature change to `bulk_search_products`:
`searches` (1-25, as today), `location_id`, and new `detail: "shortlist" | "full"` (default `"shortlist"`).

- Shortlist: for each term, fetch up to the term's `limit` products from Kroger (default stays 10, so
  there is something to choose from), rank them, return the top 3 as one line each. Per-term `limit`
  now means "candidates to consider", and the docstring says so.
- One line per option, ID first so an agent can pass it straight to the cart tools:
  `0001111041700 | Kroger 2% Reduced Fat Milk | 1 gal | $3.49 | sale $2.99 | HIGH`
  Fields: product ID, description (brand included if not already in the description), size, regular
  price, sale price only when on sale, stock level (HIGH/LOW/TEMPORARILY_OUT_OF_STOCK, or `?` if absent).
- Result per term: `{"term", "success", "count", "options": [lines], "more": <candidates not shown>}`.
  No results: `success: true`, `options: []`, with a message saying that an empty result does not mean the
  item is not stocked and an alternate term is worth trying. Failure semantics stay as today (per-term error
  object; errors that would fail every search, such as a rate limit, stop the rest and mark them `searched: false`).
- Ranking (builder may tune, but must stay deterministic and documented): keep Kroger's relevance order,
  but move TEMPORARILY_OUT_OF_STOCK items below in-stock ones. Sale status does not reorder. Reason: the
  helper this replaces scored sale/stock heavily on a limit-1 search and never detected sales; relevance order
  plus honest display lets the agent decide.
- Sale detection uses the existing `_format_pricing` (promo is a number; 0 or absent means no sale).
- Parallelism: run the terms on a small thread pool (default 5 workers), results in input order. The
  rate-limit stop rule must survive: once one search hits a stop-everything error, queued searches are
  not started. Verify the shared client is safe to call from several threads before relying on it; otherwise
  serialize the HTTP call and keep only formatting parallel.
- `detail: "full"`: today's behavior (full `_format_product` records), allowed for at most 3 terms; more is an
  `invalid_arguments` error whose `next_step` says to use the shortlist or split the call.
- Docstring and server instructions say: shortlist first, decide with the user when options differ, then add;
  added items cannot be removed from the real cart.
- Dropped from the old helper: `skip` and `quantities` (the caller omits items; quantities go on the cart call).

### 3. In-server token refresh

- A background task starts with the server (FastMCP lifespan) and runs for both stdio and HTTP transports.
  Blocking calls run off the event loop (`anyio.to_thread`).
- Cycle: at startup, then every `KROGER_REFRESH_INTERVAL_SECONDS` (default 900; 0 disables). If there is no
  saved user token, do nothing and do not log noise. Otherwise, inside kroger-api's token-file lock: re-read the
  file, and if another process already renewed it more recently than this cycle's cutoff, reuse that;
  else refresh with the saved refresh token and write the file atomically, keeping a new refresh token if
  Kroger returns one and the old one if it does not. Invalidate the cached user client after a refresh.
- Failure handling: network errors and 5xx/429 retry with backoff (3 tries inside the cycle), then wait for
  the next cycle. A definitive rejection of the refresh token (`invalid_grant` / 400-class from Kroger)
  stops the loop, logs once at error level, and records `signin_needed` with a timestamp and Kroger's reason.
  Never log tokens.
- `get_authentication_info` reports: signed in or not, access-token expiry, last successful refresh time,
  `refresh_status` (`ok`, `retrying`, `signin_needed`), and, when sign-in is needed, the next step
  (`start_authentication`). Cart and profile tools already return `authentication_required`; they keep doing so.
- A successful `complete_authentication` clears `signin_needed` and restarts the loop if it had stopped.
- Status lives in memory in one module (`refresh_status`, last success time, `signin_needed` reason and time).
  The hosted deployment is one process, so no status file or cross-process sharing. Several processes sharing
  a token file (desktop use) still share the token safely through the lock; each reports its own status.
  After a restart the status is `unknown` until the first cycle runs at startup, which retries the saved token once.
- Every path that learns the sign-in is dead updates the same status: the loop, and the on-demand renewal in
  `get_authenticated_client` (an `invalid_grant` there sets `signin_needed` and stops the loop). `sign_out`
  (`force_reauthenticate`) resets status to "not signed in". The goal is that `get_authentication_info` never says
  `ok` while cart tools return `authentication_required`.
- Whether to refresh every cycle or only when the access token has under N minutes left is the builder's
  call, pending the unverified facts above. Default: refresh every cycle (matches the proven 15-minute job
  against a 30-minute token); the rotation behavior decides if that is safe, and the builder must test it
  against a real sign-in before declaring it done (the operator provides a fresh sign-in).

### 4. Container packaging

- `Dockerfile` (multi-stage, `python:3.12-slim`, arm64-friendly, runs as non-root), `.dockerignore`,
  default command `kroger-mcp --transport streamable-http --host 0.0.0.0 --port 8000`; MCP endpoint `/mcp`.
- Env var names documented in the README: `KROGER_CLIENT_ID`, `KROGER_CLIENT_SECRET`,
  `KROGER_REDIRECT_URI`, `KROGER_TOKEN_DIR` (mount a volume, e.g. `/data`), `KROGER_USER_ZIP_CODE` (optional),
  `KROGER_USER_SCOPES` (optional), `KROGER_REFRESH_INTERVAL_SECONDS` (optional).
- A `/health` HTTP route (fastmcp custom route) for the container healthcheck; it must not call Kroger.
  It is a liveness check: always 200 while the process serves requests, with the refresh status in the body for
  humans. It never fails on `signin_needed`, because an orchestrator restart cannot fix a dead sign-in and
  would only drop a half-finished one. Sign-in state is read through `get_authentication_info`.
- The image binds all interfaces on the assumption the operator keeps it on a private network: the server has
  no client authentication. The README says so plainly.
- Docs: README section for hosting, the shortlist behavior and the refresh behavior; CHANGELOG entry.

## Testing

- Existing suites pass after the upgrade, before features.
- Shortlist: line format, top-3 cut, out-of-stock demotion, sale detection (promo as number), input-order
  output under parallel execution, empty result message, per-term failure, rate-limit stop, `detail: "full"`
  limit, size bound for a 25-term call with realistic fixtures (assert a character budget, not just counts).
- Refresh: success path, reuse of another process's newer token, retry on network error, stop on rejected
  refresh token, status surfaced by `get_authentication_info`, clearing on `complete_authentication`,
  disabled when interval is 0, no token logged. Use a fake clock and fake kroger-api; no real network in CI.
- Annotations: every tool has explicit hints.
- Manual (needs the operator): real sign-in, one real refresh cycle, container run with a volume.

## Left to the builder

Exact fastmcp floor; pool size; how candidates are fetched if thread-safety forces serializing; health route
mechanics in v4; whether `profile.compact` scope handling needs changes; precise log wording.

## My calls to check (not yet discussed)

- Python floor 3.11 and dropping 3.10 CI (forced by FastMCP 4.2).
- Shortlist options are strings, not objects; ranking is relevance order with out-of-stock demoted.
- Per-term `limit` becomes "candidates considered"; default stays 10.
- Parallel with 5 workers; refresh every cycle; health route; non-root container.
- Reviewer-raised, settled by me: Host/Origin handling and an optional `KROGER_MCP_ALLOWED_HOSTS`; in-memory
  refresh status updated by every renewal path; `/health` is liveness only.
- In the shortlist result, `count` is the number of options shown, and `more` the candidates not shown.
- The live-refresh test is a final gate the builder cannot finish alone; it needs the operator's fresh sign-in.
- kroger-api was not readable by the reviewer; the builder must read its lock and refresh code first.
- Estimated ~10K characters for a 25-term shortlist is an estimate, not measured: the budget test will measure it.
