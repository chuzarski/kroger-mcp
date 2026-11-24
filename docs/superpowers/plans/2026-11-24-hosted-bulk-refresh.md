# Hosted Kroger MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the server to fastmcp 4, make `bulk_search_products` return a compact shortlist, keep the Kroger sign-in alive from inside the server, and package it for container hosting.

**Architecture:** Task 1 upgrades the libraries with the existing suite as the safety net. Tasks 2-4 add features in separate modules (`tools/shortlist.py`, `tools/token_refresh.py`) wired in through small edits to existing files. Task 5 covers HTTP behavior and packaging.

**Tech Stack:** Python >=3.11, fastmcp 4.x (MCP SDK v2), kroger-api 0.4.0, pytest + pytest-asyncio + requests-mock, uv, Docker.

**Spec:** `docs/superpowers/specs/2026-11-24-hosted-bulk-refresh-design.md` (read it first; this plan only records decisions the spec leaves to the plan).

## Global Constraints

- `requires-python >=3.11`; drop 3.10 from classifiers and from the CI matrix (`.github/workflows/tests.yml`).
- fastmcp floor `>=4.2.5,<5` (confirm the exact floor against what resolves); keep `kroger-api>=0.4.0`.
- Do not add a second bulk tool. `bulk_search_products` changes in place: `detail: "shortlist" | "full"`, default `"shortlist"`; `"full"` allowed for at most 3 terms.
- Shortlist: at most 3 options per term; one-line strings, product ID first; `count` = options shown, `more` = candidates not shown; per-term `limit` (default 10) = candidates considered.
- Refresh: interval env `KROGER_REFRESH_INTERVAL_SECONDS`, default 900, `0` disables; never log or return tokens; status in memory, shared by every renewal path.
- `/health` is liveness only: always 200, never calls Kroger, never fails on `signin_needed`.
- Optional `KROGER_MCP_ALLOWED_HOSTS` only if fastmcp 4 enables Host checking by default.
- No private operator details (zip, store, hostnames, credentials) in any committed file, test fixture, or doc.
- No Caddy, gateway, TLS, or client-auth work.
- Never print keys or tokens. Tests never touch the real network (the `isolated` fixture in `tests/conftest.py` enforces requests-mock).
- Commit steps below run only after the maintainer says to commit (standing rule). Until then, leave work in the tree and say so. Commit messages end with the attribution line your session was given.
- Do not spawn subagents unless the maintainer asks; use `uv` at `~/.local/bin/uv` (add to PATH in non-login shells).

## Review Focus

- A term returns zero products: `success: true`, empty `options`, message says an empty result does not mean "not stocked" (test in Task 3).
- Product with no `items`, no price, or no `inventory`: shortlist line still renders with `?` stock and no price rather than crashing (Task 3).
- 25 terms of full-size realistic fixtures stay under a fixed character budget (Task 3).
- Refresh token rejected while a cart call is also renewing on demand: status ends `signin_needed`, no loop spin, no second refresh with a burned token (Task 4).
- Server started with no saved token, then `complete_authentication` succeeds: the loop starts working without a restart (Task 4).
- Token file missing, corrupt, or lacking `refresh_token` mid-cycle: logged once, loop survives (Task 4).
- Request through a proxy with a non-localhost `Host` header gets answered by the default image (Task 5).

---

### Task 1: Upgrade to fastmcp 4 on Python 3.11+

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `.github/workflows/tests.yml`, `src/kroger_mcp/errors.py`, `src/kroger_mcp/tools/shared.py`, `src/kroger_mcp/tools/product_tools.py` (imports), and any other file the upgrade breaks
- Test: existing `tests/` (no new test file)

**Interfaces:**
- Produces: a green existing suite on fastmcp 4; `log_to_client(ctx, message, level="info")` keeps its signature (Tasks 3-4 call it).

- [ ] **Step 1: Baseline.** Run `uv run --python 3.12 pytest -q` on the untouched tree. Expected: all pass. Record the count.
- [ ] **Step 2: Read the upgrade guides** (`docs/getting-started/upgrading/from-fastmcp-3.mdx`, `from-fastmcp-4-1.mdx` at tag v4.2.5 of jlowin/fastmcp) in full, and kroger-api's `authorization.py`, `token_storage.py`, `utils/locking.py` from the installed package. Write down (in your head or scratchpad, not the repo) what Task 4 needs: the refresh call, how the lock is taken, what a rejected refresh token raises.
- [ ] **Step 3: Bump dependencies.** Set `requires-python = ">=3.11"`, `fastmcp>=4.2.5,<5`, remove the 3.10 classifier, drop 3.10 from the CI matrix (including lowest-dependency jobs, whose floor must be raised to match). Run `uv lock --upgrade-package fastmcp --upgrade-package mcp`.
- [ ] **Step 4: Fix breakage** until the baseline count passes again, touching only what the guides require. Known spots: `errors.py` (`mcp.types`, `ToolResult(is_error=...)`, middleware, `ValidationError`), `log_to_client` in `shared.py` (replace the deprecated `ctx.info`/`ctx.warning` bridge with the v4 replacement; keep best-effort behavior and the existing tests in `test_concurrency.py` meaningful), the `Image` import. Run `uv run pytest -q -W error::DeprecationWarning` to find leftover SDK deprecations; fix ours, ignore third-party ones.
- [ ] **Step 5: Verify.** `uv run --python 3.12 pytest -q` and `uv run --python 3.14 pytest -q`. Expected: same count, all pass.
- [ ] **Step 6: Commit** (when the maintainer says so): `build: upgrade to fastmcp 4 and Python 3.11+`.

### Task 2: Explicit tool annotations

**Files:**
- Modify: every `@mcp.tool()` in `src/kroger_mcp/tools/*.py`
- Test: `tests/test_annotations.py` (create)

**Interfaces:**
- Produces: every registered tool has `readOnlyHint` and `destructiveHint` set explicitly (not left to defaults).

Classification (from the spec): read-only = searches, lookups, info, `view_*`; destructive = tools that clear/remove local cart or order records or delete the sign-in (`clear_current_cart`, `remove_from_cart`, `force_reauthenticate`, and any similar one you find: read each tool's body); everything else (add to cart, set preferences, start/complete authentication, mark order placed) is `readOnlyHint=False, destructiveHint=False`.

- [ ] **Step 1: Write the failing test** `test_every_tool_has_explicit_annotations`: list tools through `Client(create_server())`; for each, assert `tool.annotations` is not None and both `readOnlyHint` and `destructiveHint` are bools. Also `test_cart_clearing_tools_are_destructive` asserting the three named tools above have `destructiveHint is True`, and `test_search_tools_are_read_only` for `search_products`, `bulk_search_products`, `get_product_details`.
- [ ] **Step 2: Run** `uv run pytest tests/test_annotations.py -v`. Expected: FAIL.
- [ ] **Step 3: Add `annotations={...}` to each `@mcp.tool(...)`** per the classification. If the v4 spelling differs from `annotations=ToolAnnotations(...)`/dict, follow the guide.
- [ ] **Step 4: Run** the whole suite. Expected: PASS.
- [ ] **Step 5: Commit** (when the maintainer says so): `feat: annotate tools as read-only or destructive`.

### Task 3: Shortlist bulk search

**Files:**
- Create: `src/kroger_mcp/tools/shortlist.py`
- Modify: `src/kroger_mcp/tools/product_tools.py` (`ProductSearch` stays; `bulk_search_products` at ~line 383), server `instructions` in `src/kroger_mcp/server.py`
- Test: `tests/test_bulk_shortlist.py` (create)

**Interfaces:**
- Produces in `shortlist.py`:
  - `STOCK_ORDER: dict[str, int]` ranking stock levels (HIGH, LOW, TEMPORARILY_OUT_OF_STOCK; unknown last-but-before-out-of-stock is the builder's call, document it).
  - `option_line(product: dict) -> str`: takes a *formatted* product (output of `_format_product`) and returns `"<id> | <description incl. brand if absent> | <size> | $<regular> | sale $<sale> | <STOCK>"`. Sale segment only when `pricing.on_sale`; missing size/price render as `?`/omitted; stock from `item.inventory.stockLevel`, else `?`.
  - `shortlist_for_term(products: list[dict], top: int = 3) -> tuple[list[str], int]`: stable-sorts so TEMPORARILY_OUT_OF_STOCK items go after the rest (relevance order otherwise kept), returns `(lines[:top], more)` where `more = len(products) - len(lines)`.
- Consumes: `_format_product`, `_format_pricing`, `_PER_SEARCH_ERRORS`, `get_client_credentials_client`, `resolve_location_id`, `log_to_client`.
- Produces in `product_tools.py`: `bulk_search_products(searches, location_id=None, detail: Literal["shortlist","full"]="shortlist", ctx=None)`; per-term result `{"term","success","count","options":[str],"more":int}` in shortlist mode (plus `message` when empty); full mode keeps today's result shape.

- [ ] **Step 1: Failing unit tests** in `tests/test_bulk_shortlist.py` for `option_line` and `shortlist_for_term`: line format with the spec's example (`0001111041700 | Kroger 2% Reduced Fat Milk | 1 gal | $3.49 | sale $2.99 | HIGH`); no sale segment when `promo` is 0 or absent; `?` stock when inventory absent; out-of-stock demoted but relevance order otherwise preserved; top-3 cut and `more`.
- [ ] **Step 2: Failing tool tests** using the `kroger` fixture style from `tests/test_concurrency.py` (requests-mock products endpoint): shortlist result shape; empty result message; output order equals input order when responses arrive out of order (use delayed mocks); one term failing with a per-term error while others succeed; a rate-limit error stops queued terms (`searched: false`) and the call fails only if nothing succeeded (existing semantics); `detail="full"` with 4 terms returns an `invalid_arguments` error whose next_step mentions the shortlist; `detail="full"` with 3 terms returns full records; **size budget**: 25 terms x 10 realistic full-size products (include nutrition, images, allergens) produce a result whose JSON text is under 15,000 characters (measure first; set the budget at about 1.5x the measured size and record the measured size in the test comment).
- [ ] **Step 3: Run** `uv run pytest tests/test_bulk_shortlist.py -v`. Expected: FAIL.
- [ ] **Step 4: Implement `shortlist.py`** to the signatures above.
- [ ] **Step 5: Rework `bulk_search_products`.** Validate terms/brands up front as today; reject `detail="full"` with more than 3 terms via the project's invalid-arguments error path (see how `validation`/`KrogerToolError` raise it elsewhere). Run searches on a `ThreadPoolExecutor(max_workers=5)` and collect in input order. A stop-everything error (anything outside `_PER_SEARCH_ERRORS`) must prevent not-yet-started searches from running. First read whether the shared `KrogerAPI` client is safe from several threads (requests session, token refresh inside calls); if not, keep the HTTP call under a lock and parallelize nothing else, and say so in a code comment. Update the docstring (shortlist meaning of `limit`, `detail`, "decide with the user when options differ, added items cannot be removed from the real cart") and the server `instructions` bulk workflow line.
- [ ] **Step 6: Run** the full suite. Expected: PASS, including the budget test.
- [ ] **Step 7: Commit** (when the maintainer says so): `feat: shortlist output for bulk_search_products`.

### Task 4: In-server token refresh

**Files:**
- Create: `src/kroger_mcp/tools/token_refresh.py`
- Modify: `src/kroger_mcp/server.py` (lifespan), `src/kroger_mcp/tools/shared.py` (`get_authenticated_client`, `sign_out` update status), `src/kroger_mcp/tools/auth_tools.py` (`complete_authentication` clears status and wakes the loop), `src/kroger_mcp/tools/profile_tools.py` (`get_authentication_info` fields)
- Test: `tests/test_token_refresh.py` (create)

**Interfaces:**
- Produces in `token_refresh.py`:
  - `class RefreshStatus` (module-level singleton `status`) with `state: Literal["unknown","ok","retrying","signin_needed","signed_out"]`, `last_success: float | None`, `reason: str | None`, `since: float | None`, and `as_dict() -> dict` (no secrets). Methods `mark_ok()`, `mark_retrying(reason)`, `mark_signin_needed(reason)`, `mark_signed_out()`.
  - `refresh_once(now: Callable[[], float] = time.time) -> str`: one cycle under kroger-api's token-file lock; returns `"no_token" | "reused" | "refreshed"`; raises the kroger-api error on failure. Re-reads the file inside the lock and returns `"reused"` if another process wrote a newer token than this cycle's cutoff; atomic write, keeps the old refresh token if Kroger returns none; calls `invalidate_authenticated_client()` after a refresh.
  - `async def run_refresh_loop(interval: float, sleep=anyio.sleep, wake: anyio.Event | None = None) -> None`: startup cycle, then every `interval`; network/5xx/429 errors retry 3 times with backoff then wait for the next cycle (`mark_retrying`); a definitive rejection (the kroger-api error Task 1 Step 2 identified for `invalid_grant`) calls `mark_signin_needed`, logs once at error level, and parks until `wake` is set (set by `complete_authentication`); no token: `mark_signed_out`, keep waiting; corrupt/missing refresh token: log once, keep going. Blocking work runs via `anyio.to_thread.run_sync`.
  - `refresh_interval() -> float` reads `KROGER_REFRESH_INTERVAL_SECONDS` (default 900; invalid value falls back to 900 with one warning; `0` means the loop is not started).
- Consumes: `invalidate_authenticated_client`, `USER_TOKEN_FILE`, `load_token`, `file_lock` from kroger-api, `log_to_client` not needed (server-side logging via `logging`).
- `get_authentication_info` result gains `refresh: status.as_dict()` and, when `state == "signin_needed"`, `authenticated: False` with the `SIGN_IN_STEP` next step.

- [ ] **Step 1: Failing tests** in `tests/test_token_refresh.py` with a fake `sleep` and requests-mock for the token endpoint (follow `save_user_token` in conftest): `refresh_once` refreshes and writes the file with the new access token; keeps the old refresh token when the response has none; returns `"reused"` when the file already holds a newer token; returns `"no_token"` with no file; loop retries a network error 3 times then waits for the next cycle and marks `retrying`; loop stops on a rejected refresh token, marks `signin_needed` with Kroger's reason, makes no further token requests until `wake` is set; after `wake` it resumes; interval `0` starts no loop; invalid interval value falls back to 900; no secret appears in `caplog` text or `status.as_dict()`; `get_authentication_info` shows the refresh status and does not say authenticated when `signin_needed`; an `invalid_grant` raised in `get_authenticated_client` (on-demand renewal) sets `signin_needed`; `sign_out` sets `signed_out`; `complete_authentication` success clears `signin_needed` and sets the wake event; a Client-driven test that the server lifespan starts the loop once and cancels it on shutdown.
- [ ] **Step 2: Run** `uv run pytest tests/test_token_refresh.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `token_refresh.py` to the interfaces, using the refresh call and lock helper found in Task 1 Step 2. Do not invent a second locking scheme; use `file_lock`.
- [ ] **Step 4: Wire it in** per the Files list. The lifespan starts the loop in a task group when `refresh_interval() > 0`, for stdio and HTTP alike, and cancels it at shutdown. Make sure `create_server()` called repeatedly in tests does not leak loops or share the wake event across servers (reset in the `isolated` fixture: add `monkeypatch` resets for `token_refresh.status` and the wake event to `tests/conftest.py`).
- [ ] **Step 5: Run** the full suite. Expected: PASS.
- [ ] **Step 6: Live gate (needs the maintainer).** With a fresh sign-in from the maintainer in a scratch `KROGER_TOKEN_DIR`, run the server with `KROGER_REFRESH_INTERVAL_SECONDS=60`, watch two cycles succeed in the logs, call `get_authentication_info`. Check whether Kroger rotates the refresh token and write the finding in `CHANGELOG.md`/README. If rotation makes refresh-every-cycle unsafe, switch to refreshing only when under N minutes remain and update the spec. This step cannot be finished without the maintainer; report it as pending until then.
- [ ] **Step 7: Commit** (when the maintainer says so): `feat: refresh the Kroger token from inside the server`.

### Task 5: HTTP behavior and container packaging

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `tests/test_http.py`
- Modify: `src/kroger_mcp/server.py` or `cli.py` (health route, allowed hosts), `README.md`, `CHANGELOG.md`, `.env.example`
- Test: `tests/test_http.py`

**Interfaces:**
- Produces: `GET /health` -> 200 JSON `{"status":"ok","refresh": status.as_dict()}`; image whose default command is `kroger-mcp --transport streamable-http --host 0.0.0.0 --port 8000`, serving `/mcp`.
- Consumes: `token_refresh.status` (Task 4).

- [ ] **Step 1: Failing tests** in `tests/test_http.py` using the ASGI app of the server (httpx ASGI transport or fastmcp's test helper, per the v4 docs): `/health` returns 200 with no Kroger request made (requests-mock asserts zero calls) and still 200 when status is `signin_needed`; an MCP `initialize` POST to `/mcp` with `Host: mcp.example.test` and an `Origin: https://mcp.example.test` header succeeds with the default configuration. If fastmcp 4 rejects it, add the failing assertion for `KROGER_MCP_ALLOWED_HOSTS=mcp.example.test` accepting it and `*` accepting any, and for an unlisted host being refused when the variable is set to a specific list.
- [ ] **Step 2: Run** `uv run pytest tests/test_http.py -v`. Expected: FAIL (at least `/health`).
- [ ] **Step 3: Implement** the health route (fastmcp custom route; liveness only) and, only if Step 1 showed the default rejects proxied hosts, `KROGER_MCP_ALLOWED_HOSTS` parsing (comma-separated, `*` disables).
- [ ] **Step 4: Dockerfile.** Multi-stage on `python:3.12-slim`: build the wheel/venv with `uv` in a builder stage, copy to a runtime stage, create and use a non-root user, `ENV KROGER_TOKEN_DIR=/data`, `VOLUME /data`, `EXPOSE 8000`, `HEALTHCHECK` using Python's `urllib` against `/health` (no curl in slim), the default command above. `.dockerignore` excludes `.git`, `HANDOFF.local.md`, `docs`, tests, `.venv`, `*.json`, env files.
- [ ] **Step 5: Verify the image.** Build the image (arm64 is the target architecture). Run with dummy `KROGER_CLIENT_ID`/`KROGER_CLIENT_SECRET`, an empty volume, and `KROGER_REFRESH_INTERVAL_SECONDS=0`; expected: healthcheck turns healthy, `/health` returns 200, an MCP client lists the tools (annotations present), container user is not root. Use dummy credentials only; never real ones in commands or logs.
- [ ] **Step 6: Docs.** README hosting section: image, env var names (`KROGER_CLIENT_ID`, `KROGER_CLIENT_SECRET`, `KROGER_REDIRECT_URI`, `KROGER_TOKEN_DIR`, `KROGER_USER_ZIP_CODE`, `KROGER_USER_SCOPES`, `KROGER_REFRESH_INTERVAL_SECONDS`, and `KROGER_MCP_ALLOWED_HOSTS` if added), the volume, `/health` meaning, a plain statement that the server has no client authentication and must sit on a private network behind something that authenticates, the shortlist behavior, the refresh behavior and `signin_needed`. CHANGELOG entry covering Tasks 1-5. Update `.env.example` for new variables.
- [ ] **Step 7: Run** full suite on 3.12 and 3.14. Expected: PASS.
- [ ] **Step 8: Hand back to the operator** (who wires the image behind a proxy): image build context, port 8000, endpoint `/mcp`, env var names, volume path `/data`, healthcheck. No secrets in the handoff.
- [ ] **Step 9: Commit** (when the maintainer says so): `feat: health route and container image for hosting`.
