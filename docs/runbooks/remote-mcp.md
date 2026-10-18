# Remote MCP (Clerk)

> **Retired 2026-10-18.** This hosted server was never deployed; its Railway service was
> removed. Remote MCP access now goes through the Cloudflare Worker tool hub
> (`bookchaowalit/platform-mcp-remote-worker`: `list_cells`, `describe_cell`, `call_cell` over
> every cell, tasks included). The local stdio `empire-mcp` is unaffected. The text below is
> kept as the design record in case a Clerk-backed server is wanted again.

`empire-mcp` serves the `tasks.*` tools (and `graph.*` when a graph API is
configured) over MCP Streamable HTTP, so Claude on the web, on mobile, and in
cloud agents can connect. Clerk is the OAuth 2.1 authorization server; this
service is only the resource server. Code: `services/mcp/empire_mcp/hosted.py`
(console script `empire-mcp-http`, entrypoint `EMPIRE_SERVICE=mcp`).

Status: built and tested locally; not deployed yet.

## Request path

1. The client calls `https://<public host>/mcp`. Without a token it gets 401
   with `WWW-Authenticate: Bearer ... resource_metadata=".../.well-known/oauth-protected-resource/mcp"`.
   That metadata names the Clerk issuer as the authorization server; the client
   registers itself there (DCR) and signs the user in with PKCE.
2. Before Clerk: a bearer that is empty, longer than 4 KB, or contains
   whitespace or control characters is rejected without a call. A client IP
   with 30 failed (401/403) attempts in the last minute gets 429 until the
   window passes (in-memory, per process). On Railway (`RAILWAY_ENVIRONMENT*`
   set) the IP is the left-most `X-Forwarded-For` entry; elsewhere the socket
   peer.
3. Every remaining token is checked with Clerk's Backend API
   `POST https://api.clerk.com/v1/oauth_applications/access_tokens/verify`
   (5 s timeout). Until one token has verified, a 404 there is retried once
   without `/v1`; the path that answered 200 is remembered. A 404 is never
   success. A non-200 answer, a response whose `object` is present but not an
   OAuth access token, a revoked or expired token, or a missing subject is
   invalid. With `EMPIRE_MCP_ALLOWED_CLIENTS` set, a token whose `client_id`
   is missing or not listed is invalid (401) before the allowlist step.
   Positive results are cached for at most 60 s (never past the token's
   expiry), negative ones for 15 s, both under a SHA-256 of the token and
   bounded in size; a revocation takes effect within 60 s.
4. Allowlist: the Clerk user id must be a key of `EMPIRE_MCP_SUBJECTS`.
   Anyone can sign up to Clerk, so this map is the gate. Any other valid
   token gets 403 `insufficient_access` before any tool runs; an empty or
   missing map allows nobody. A malformed map stops the service at start.
5. Each tool call mints a token with empire-mcp's own RS256 key:
   `iss=empire-mcp`, `aud=tasks-api`, `sub` = mapped identity, `ws` = mapped
   workspace, `act.sub = "clerk:<user id>"`, `scope` = `tasks.task:read
   tasks.task:write tasks.status:write tasks.link:write`, lifetime 300 s, no
   `del`. It calls the Tasks API with channel `mcp` and a fresh
   `X-Correlation-Id` (`mcp-...`).
6. The Tasks API trusts that issuer only with its own public key, only for a
   `scope` inside those four scopes, never with `del`/`obo`, for at most
   300 s, and only on channel `mcp`; with `TASKS_API_MCP_SUBJECTS` set, the
   token's (`sub`, `ws`) must also be one of the listed pairs. Anything else is
   401. The scope claim only
   narrows: the mapped identity still needs the permission in its workspace,
   so a viewer cannot write. Deleting and archiving are not reachable. The
   execution records `executed_by` = `initiated_by` = mapped identity and
   channel `mcp`; the `clerk:<user id>` actor appears as `external_actor` in
   the Tasks API's structured write log.

Tokens, the Clerk secret key, and the signing key are never logged.

## Variables (names only)

`empire-mcp` service:

| Variable | Meaning |
|---|---|
| `EMPIRE_SERVICE` | `mcp` |
| `EMPIRE_MCP_PUBLIC_URL` | public origin, e.g. `https://<host>` (no path; the endpoint is `<origin>/mcp`). Also the only accepted `Host`/`Origin` (DNS-rebinding protection) |
| `CLERK_ISSUER_URL` | the Clerk Frontend API URL (authorization server) |
| `CLERK_SECRET_KEY` | Clerk Backend API secret key, used only for the verify call |
| `EMPIRE_MCP_SIGNING_KEY_PEM` | empire-mcp's private key (PEM). Not the automation key |
| `EMPIRE_MCP_SUBJECTS` | JSON `{"<clerk user id>": {"identity": "<platform identity uuid>", "workspace": "<workspace uuid>"}}` |
| `EMPIRE_MCP_ALLOWED_CLIENTS` | recommended; comma list of Clerk OAuth client ids allowed to present tokens (see "Pin Claude's client"). Unset: pinning off, one warning at start |
| `EMPIRE_TASKS_API` | Tasks API base URL; `https://...`, or `http://` only for `*.railway.internal` or loopback |
| `EMPIRE_GRAPH_API` | optional, same URL rule; graph tools are registered only when set |
| `CLERK_API_URL` | optional; Clerk Backend API base, default `https://api.clerk.com/v1` |
| `EMPIRE_MCP_ISSUER` | optional; default `empire-mcp` |
| `PORT` | set by Railway; the server binds `0.0.0.0:$PORT` |

`tasks-api` service (in addition to its existing variables):

| Variable | Meaning |
|---|---|
| `TASKS_API_MCP_PUBLIC_KEY_PEM` | empire-mcp's public key; without it MCP tokens are refused |
| `TASKS_API_TRUSTED_MCP_ISSUER` | optional; default `empire-mcp`; must equal `EMPIRE_MCP_ISSUER` |
| `TASKS_API_MCP_SUBJECTS` | recommended; JSON list `[{"sub": "<identity uuid>", "ws": "<workspace uuid>"}]`, the same pairs as `EMPIRE_MCP_SUBJECTS`. MCP tokens for any other pair are 401, so a leaked MCP signing key cannot act as anyone else. `[]` refuses every MCP token; ignored while `TASKS_API_MCP_PUBLIC_KEY_PEM` is unset |

Any change to a Railway variable takes effect only after the service is
redeployed.

In hosted mode the Tasks API refuses to start if the MCP issuer equals the main
or the dev issuer, if the MCP public key equals the main public key, or if it
is the dev public key.

## Keys

Generate a separate pair on the operator machine, never on Railway, with the
existing generator (refuses to overwrite; private key 0600 in a 0700 directory):

```bash
empire-automations keys --dir ~/.config/empire-platform/mcp-signing   # prints the public key path
```

Put `private.pem` into `EMPIRE_MCP_SIGNING_KEY_PEM` (empire-mcp) and
`public.pem` into `TASKS_API_MCP_PUBLIC_KEY_PEM` (tasks-api) through Railway
variables. Rotating: generate a new pair in a new directory, update both
variables, redeploy tasks-api first, then empire-mcp. Tool calls fail between
the two redeploys (tasks-api already expects the new key); tokens live 300 s,
so nothing older survives.

## Clerk settings

- A **separate Clerk application (instance) used only for MCP**, so its users,
  OAuth clients, and secret key are not shared with any other app.
- OAuth applications: Dynamic Client Registration **on** (Claude registers
  itself), with **"Show consent screen for dynamically registered clients"**
  (or Clerk's equivalent consent setting) **on**, so a user always sees which
  client asks for access.
- PKCE **required** for public clients.
- Access tokens **opaque** (verified through the verify endpoint, so
  revocation is immediate up to the 60 s cache).
- Sign-up **restricted** (allowlist or invitation only). The
  `EMPIRE_MCP_SUBJECTS` map is the real gate either way: only mapped user ids
  reach a tool.

Verify the Backend API path once with the real key (prints only the path
status, never the key):

```bash
CLERK_SECRET_KEY=... empire-mcp-http --check-clerk
# https://api.clerk.com/v1/oauth_applications/access_tokens/verify: path ok (4xx for a bogus token, ...)
```

`path ok` with 400/401/422 means the route exists; `path not found (404 ...)`
means it does not. If both lines say 404, read the Clerk error codes shown:
a `resource_not_found` code may also be Clerk's answer for an unknown token.

## Pin Claude's client

1. Deploy with `EMPIRE_MCP_ALLOWED_CLIENTS` unset (the start log warns that
   client pinning is off).
2. Connect from Claude once with a mapped account. Claude registers a client
   through DCR.
3. In the Clerk dashboard (OAuth applications), find the dynamically
   registered client for Claude and copy its client id. The empire-mcp log
   also records `client_id` on `mcp.auth.rejected` lines once pinning is on.
4. Set `EMPIRE_MCP_ALLOWED_CLIENTS` to that id (comma-separate several, e.g.
   one per Claude surface if they register separately) and redeploy
   empire-mcp. Tokens from any other client then get 401.
5. If Claude registers a new client later (it shows up as 401 with
   `client_not_allowed`), check it in Clerk and add its id the same way.

## Checks after deploy

- `GET /health` answers 200.
- `POST /mcp` without a token answers 401 with `resource_metadata` in
  `WWW-Authenticate`.
- `GET /.well-known/oauth-protected-resource/mcp` lists the Clerk issuer.
- Connect from Claude with a mapped account: `tasks.list` works; an unmapped
  Clerk account gets 403.

## Kill switch and rollback

Every step needs a redeploy of the named service; a Railway variable change
alone does nothing. Do them in this order:

1. Unset `TASKS_API_MCP_PUBLIC_KEY_PEM` on tasks-api and redeploy tasks-api.
   This is the immediate cut: every MCP-issued token is refused.
   Main-issuer tokens are unaffected.
2. Empty `EMPIRE_MCP_SUBJECTS` on empire-mcp and redeploy empire-mcp: nobody
   may use tools (or remove the service).
3. In Clerk, turn Dynamic Client Registration off and revoke the OAuth access
   tokens and the OAuth clients (applications) that were registered.
4. Rotate the MCP key pair (see Keys) before trusting the issuer again.

Tests: `tests/test_remote_mcp.py`.
