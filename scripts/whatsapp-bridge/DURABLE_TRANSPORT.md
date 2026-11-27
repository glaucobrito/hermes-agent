# Durable group transport (unit A1)

<!-- #atende:SPEC-licencas-acompanhamento/REQ-R4 REQ-R5 REQ-R8 REQ-R2 -->

Nina acceptance (2026-11-27): canonical `/codex:review` job `review-mwyjn1i3-soa4ff`,
thread `01a21b7c-a510-75c3-91a3-90297d7b7eef`, found zero P0/P1. The five P2
and one P3 findings remain debt (listed below), not a new correction round under
Glauco's explicit threshold. The reviewer could not run filesystem tests in its
read-only sandbox; Nina independently ran the full suite with real Express 4.22.1:
55/55 passed, zero skips (`/home/clawd/licencas-hermes-evidence/nina-eager-fresh.log`),
plus syntax and staged diff checks. This is local A1 acceptance, NOT production
rollout, a live Baileys smoke or a working business pilot. Effective-profile/YAML
forwarding is an A2 integration responsibility; this opt-in bridge uses process env.


Opt-in, at-least-once inbound spool plus an idempotent send ledger for **explicitly
configured WhatsApp groups**, inside the existing bridge (`bridge.js` +
`durable-inbox.js`). It is technical persistence only: no business state and no
SQLite. With `WHATSAPP_DURABLE_GROUP_IDS` unset, the bridge behaves exactly as before.

## Configuration (env, non-secret)

| Variable | Default | Bounds | Intent |
|---|---|---|---|
| `WHATSAPP_DURABLE_GROUP_IDS` | unset = disabled | JSON array, 1..32 exact `<digits>@g.us` | Trust boundary: only these groups are spooled, claimable, or sendable in attempt mode |
| `WHATSAPP_DURABLE_SPOOL_DIR` | `$HERMES_HOME/whatsapp/durable`, else `~/.hermes/whatsapp/durable` | absolute path | Spool location (dirs `0700`, files `0600`). Set it explicitly when Hermes profiles are in use |
| `WHATSAPP_DURABLE_LEASE_MS` | `120000` | 5000..900000 | Long enough for the consumer to commit its own inbox before acking, short enough that a crashed consumer's messages come back quickly |
| `WHATSAPP_DURABLE_RETENTION_DAYS` | `35` | 31..3650 | How long acked tombstones and confirmed ledger entries are kept. The minimum is above WhatsApp's 30-day offline redelivery window |
| `WHATSAPP_DURABLE_MAX_TEXT_CHARS` | `8192` | 256..65536 | Caps stored text per message, counted in code points. Truncation is recorded |

**Fail closed:** if any of these is invalid, if an unknown `WHATSAPP_DURABLE_*`
name is set, or if a durable variable is set without `GROUP_IDS`, the bridge
refuses to start (exit 1). It never falls back to legacy for configured groups.
A tampered spool also stops startup: unexpected entries, symlinks, or corrupt
records. Crash leftovers in `tmp/` are discarded at startup only when **every**
entry there has the exact name the atomic writer generates
(`<64 hex>.json.<16 hex>.tmp`) and is a private regular file (owned by the bridge
user, mode without group/other bits, single link). Any other entry (another name,
a symlink, a directory, a hard link, a readable file) fails startup closed and
nothing in `tmp/` is deleted, whatever its position in the directory listing.

## Startup, ownership and first provisioning

Startup order: provision the spool root → fsync its real ancestor chain → take exclusive ownership → provision
`inbox/`, `sends/`, `tmp/`, `account/` → discard tmp files → load → check the
account binding → purge → start maintenance.
Any failure throws before anything is cleaned, loaded, served or sent, and the
bridge exits 1 before its HTTP listener or WhatsApp socket starts.

- **Exclusive ownership.** The bridge takes a Linux kernel advisory lock (`flock(2)`) on
  `<spool>/owner.lock`. The lock file is opened `O_NOFOLLOW`, must be a private regular
  file, and is kept at `0600`. util-linux `flock(1)` runs on that open fd as a short-lived
  helper with no shell and no PID file. flock locks belong to the open file description,
  so the lock stays with the bridge after the helper exits. The kernel releases it when
  the bridge dies for any reason, SIGKILL included. A second live process sharing the
  spool fails with `DurableOwnershipError` and touches nothing.
  - There is no stale-lock detection and no stealing.
  - Startup also checks, with a second open file description, that the lock really
    conflicts on that filesystem, and fails closed if it does not.
  - A successor started while its predecessor is still exiting fails closed rather than
    waiting. Restart orchestration is A2.
  - Pair-only mode with durable variables set takes the same lock.
- **First-provision durability.** Each missing spool directory component, including new
  ancestors such as `$HERMES_HOME/whatsapp`, is created one at a time with mode `0700`. It
  is checked with `lstat` (no symlink, owned by the bridge user) and its entry is fsynced
  into its parent before the next step.
  - Pre-existing ancestors are fsynced but never chmod'ed.
  - On every start, every directory of the spool's real path (`realpath`), from its parent
    up to `/`, is fsynced before ownership is taken. This covers ancestors created before
    startup by someone else: on a fresh install `bridge.js` runs
    `mkdirSync(SESSION_DIR, { recursive: true })` first, so `~/.hermes` and
    `~/.hermes/whatsapp`, shared with the default spool, already exist and were never
    fsynced. It also heals a crash between an earlier mkdir and its parent fsync.
  - An fsync failure (including on any of those ancestors) aborts startup before any effect.
- **Maintenance timer.** A plain `setInterval` (60 s) inside the bridge process, not a Hermes
  cron job. It is `unref()`'d, so it never keeps the process alive, and `close()` (also called
  on process `exit`) clears it and releases the lock. Each tick does three things:
  - retries buffered inbound writes;
  - verifies that the on-disk records mirror the owner's state;
  - purges expired records.

  Retention therefore no longer depends on a consumer claiming or on a restart.
- **Quarantine.** If a tick finds tampered state (an unexpected or non-regular entry, an
  unknown record, a record that vanished) or a purge fails, the spool is quarantined:
  - claim, ack, send and receipt return `503 durable_quarantined`;
  - ingest is not persisted;
  - legacy routes stay denied for configured groups;
  - `/health` reports `durable.quarantined` and `durable.degraded`.

  Recovery requires an operator restart, which re-validates the spool and fails closed if
  it is still corrupt.

## Account binding

A spool belongs to exactly **one** WhatsApp account: the one Baileys reports for the
live socket (`sock.user.id`, device suffix removed), read through the existing
`getAccountJid` callback. HTTP never supplies or chooses the account. An inbound message
also carries the account of the socket that emitted it (see Inbound → Emitting socket).

- **Binding.** The first operation under a known account (ingest, claim, ack, send,
  receipt) writes `account/<sha256(account)>.json` atomically (fsync'd, `0600`)
  before anything is served. The binding is never replaced by another account.
- **Provisional binding.** If the binding file was renamed into `account/` but the
  `account/` fsync failed, the binding is only provisional: it is on disk (so it mirrors
  disk for the mismatch check and the maintenance tick), but it is not known durable.
  Every later operation repeats the whole atomic write of the same record (tmp fsync,
  rename, `account/` fsync) before it can have any effect, and returns
  `503 account_bind_failed` until that succeeds (ingest: the message goes to the inbound
  retry buffer with code `account_bind_failed`; see Inbound). While provisional, nothing is persisted, served or sent, retention purge does
  not run (the legacy account evidence is kept), and `/health` reports
  `account_bound: false`. A binding loaded at startup is durable, because `account/` is
  fsynced before loading.
- **Refusal.** While Baileys reports no account, claim, ack, send and receipt return
  `503 account_unknown`, and ingest is rejected (`account_unknown`). Under any other
  account they return `409 account_mismatch` and ingest is rejected
  (`account_mismatch`). A refused call has no effect: no lease, no ack, no ledger
  read or write, no provider call, no envelope or receipt content. This holds
  during execution and after a restart. A failed binding write returns
  `503 account_bind_failed`; for ingest it is a persist failure (see Inbound), not a
  rejection. When the bound account comes back, it works normally.
- **During a send.** Before each chunk the live account is checked again. If it changed,
  no further chunk is sent and the attempt becomes `delivery_unknown`
  (`error: account_changed`), never resent.
- **Startup checks (fail closed, nothing deleted).** A bound spool may hold only inbound
  records of its account. A spool without a binding (created before binding existed)
  may bind only to the single account its inbound records prove. Inbound records of
  more than one account, or send records with no inbound record to prove the account,
  stop startup. Retention purge does not run until the spool is durably bound.
- The binding is checked by the maintenance tick like any other record (quarantine
  if it vanishes or an unknown one appears). `/health` → `durable` reports
  `account_bound` and `account_mismatch` (booleans, no JID); a mismatch also sets
  `degraded`.

## Inbound

For a configured group, `messages.upsert` writes a record to
`inbox/<sha256(instance, group, message_id)>.json`. The write goes to a tmp file
first, which is fsynced, renamed into place, and then the directory is fsynced.
The record is never added to the destructive `/messages` queue, and media is
never downloaded. Fields come only from the Baileys event: `key.remoteJid`,
`key.participant` (LIDs stored verbatim, phone numbers never inferred), `key.id`,
`pushName`, `messageTimestamp`, and quoted `stanzaId`/`participant`. The bridge
adds `received_at`. Attachments are stored as metadata only, never `url`,
`mediaKey` or `directPath`.

Ignored, not persisted and not exposed:
- `fromMe`
- `message == null`, such as a decrypt-retry placeholder, so the real content arriving later is not blocked
- unsupported content: reactions, protocol messages, edits

Rejected and logged: invalid or traversal message ids or participants, an unknown bot
account (`account_unknown`), an account other than the spool's (`account_mismatch`), a
message emitted by a socket whose account is no longer the live one (`account_changed`),
and an ingest call without an origin captured from a socket (`origin_unknown`).

**Emitting socket.** `bridge.js` keeps `emitter`, the socket each `startSocket()` created,
and its `messages.upsert` loop iterates `routeUpsertBatch(durable, emitter, messages, log)`.
That call runs synchronously, before the loop body's first `await` (a legacy media download):
1. It captures the emitter's account once (`captureInboundOrigin`: a frozen snapshot, accepted
   only if made by that function).
2. It ingests every durable-group message of the batch under that origin, in order. Each one is
   persisted (or buffered for retry, or refused) before the call returns.
3. Only then does it return the other messages, in their relative order, for the legacy body.

No durable message therefore waits in memory behind a legacy download: a bridge exit during
the download loses none of them, and a restart under the same account serves them. A reconnect
to another account during that download comes too late to matter. The batch is already in
the spool under the emitter's account (A); the spool is bound to A, so B gets
`409 account_mismatch` and none of A's messages, and A gets them back when it reconnects.
Ingest still never reads the batch's account from the global `sock`. It compares the origin
with the live account (`getAccountJid`) and accepts the message only if they are the same
account:
- same account on a new socket (a reconnect or restart of the same session) → spooled as usual;
- another account is live → `account_changed`. Not written, the spool is not bound, and
  nothing reaches the new account's claims. This happens only for a late batch that the old
  socket emits after the bridge already reconnected as another account.

Other groups and DMs follow the legacy path unchanged.

**Spool write failure on ingest.** A failed write is never reported as persisted or
claimable, and never falls back to the legacy queue. The record is kept in a bounded
in-memory buffer (at most 64 records) and retried on each maintenance tick, and also
immediately if Baileys delivers the same message again. After 5 failed attempts, or when
the buffer is full, the record is dropped.

The account binding is part of that write. If the first message of a configured group
arrives while the binding cannot be written or fsynced, the message is buffered like any
other failed write (code `account_bind_failed`), counted in `ingest_failures`, and
retried through the same buffer and bounds. The record keeps the live account it was
received under. Each attempt first checks that Baileys still reports that same account,
and only then binds the spool to it durably and writes the record:
- another account → `account_changed`;
- no account → `account_unknown`;
- spool bound to another account in the meantime → `account_mismatch`.

Nothing is written in those three cases, and a retry never binds the spool to a
different account. The record becomes claimable only after both the binding and the
record itself are fsynced. Exhausted retries count in `ingest_dropped`.

Logs are sanitized: a status, an error code and `envelope_id` (a hash), never text, JIDs,
provider ids or raw error messages. `/health` → `durable` exposes `degraded`,
`ingest_failures`, `ingest_retry_buffered`, `ingest_recovered`, `ingest_dropped` and
`last_ingest_failure {at, code}`. `degraded` stays true while anything is buffered, and
after any drop until restart.

**SDK boundary:** the buffer is memory only. A bridge crash or power loss while a record
is buffered loses it, and Baileys does not redeliver an event after `messages.upsert`.
Nothing here gives a Baileys-level ack or a power-loss guarantee beyond the fsync'd spool.

## HTTP (loopback only, existing Host-header guard)

There is no endpoint that creates inbound envelopes, and HTTP cannot change the group list.
Request bodies with unknown fields are rejected with `400`.

| Request | Responses |
|---|---|
| `POST /durable/inbox/claim` `{limit?: 1..50}` | `200 {leases:[{envelope_id, lease_token, lease_expires_at, delivery_count, envelope}]}` |
| `POST /durable/inbox/ack` `{envelope_id, lease_token}` | `200 {status:"acked"\|"already_acked"}` · `409 lease_invalid\|lease_expired` · `404 unknown_envelope` · `500 durable_internal_error` (not acked, will replay) |
| `POST /send` `{chatId, message, replyTo?, attempt_id}` | `200 {success, attempt_id, state:"confirmed", messageId, messageIds, replayed}` · `502 delivery_unknown` (this call hit an uncertain provider outcome) · `409 delivery_unknown\|attempt_in_progress\|attempt_conflict` · `403 chat_not_configured` · `422 reply_target_not_found\|reply_target_not_quotable` · `503 not_connected` · `500 ledger_persist_failed` (nothing sent) |
| `GET /durable/send/receipt/:attempt_id` | `200 {state:"confirmed"\|"delivery_unknown"\|"in_progress", ledger_state, message_ids, chunks, ...}` · `404 unknown_attempt` |

Claim, ack, attempt send and receipt also return `409 account_mismatch` or
`503 account_unknown|account_bind_failed` (see Account binding), checked before any
other state is read.

With the transport disabled, `/durable/*` returns `404 durable_transport_disabled`,
and `/send` with an `attempt_id` returns `400`. `/send` without `attempt_id` is the
untouched legacy path.

**Legacy route guard.** `installDurableRoutes(app, durable)` is called before the legacy
handlers and registers its guards on the same Express paths. Express's own matcher
therefore decides for every variant it accepts: case-insensitive, with an optional
trailing slash, e.g. `/send/`, `/SEND?x=1`, `/Send-Media/`.

For a configured group:
- `/send` without `attempt_id` → `403 legacy_route_disabled_for_durable_group`. A task that
  started before activation cannot answer outside the ledger.
- `/edit` and `/send-media` → `403`.
- The `chatId` check also catches whitespace, case, device-suffix and array forms.

Other requests:
- `/send` with `attempt_id` to a chat that is not configured → `403 chat_not_configured` (fail closed).
- `attempt_id` on `/edit` or `/send-media` → `400 attempt_id_not_supported`.
- Chats outside the whitelist keep the legacy handlers unchanged.
- `/typing` (presence only) is not guarded.

For non-Express callers, `routeDurableRequest` uses regexes that match what Express 4
compiles. The tests check these against real Express on a hostile corpus.

**Ack contract (A2):** the consumer acks only after committing the envelope to its
own inbox, keyed by `envelope_id`, which is deterministic across replays. Lease
tokens are kept only as sha256 hashes. Lease state is held in memory, so after a
bridge restart every unacked envelope is replayed under a new token.

**Send ledger:** `sends/<sha256(attempt_id)>.json` stores an intent record before
any Baileys call. It holds a request hash, never the message text. A chunk counts
as `accepted` only when Baileys resolves with a `key.id`. `confirmed` therefore
means Baileys accepted the stanza; it does not mean the message was delivered or
read. Any error or missing evidence produces `delivery_unknown` and is never
resent. Retrying a confirmed attempt returns the original IDs. `replyTo` must be
a text message stored in the spool for the same group, and it is sent as a real
Baileys `quoted` on the first chunk.

## Known limits (left for A2/B)

- Media replies cannot be quoted (`422`), and mentions are not stored.
- Edits and revokes of spooled messages are not tracked.
- If a spool write keeps failing on ingest (5 ticks) or the bridge dies while the record is buffered, the message is lost (Baileys does not redeliver). This is loud: `durable_inbound` / `durable_inbound_retry` logs and `/health` `durable.degraded`.
- A durable message emitted by a socket whose account is no longer live (`account_changed`)
  is refused, not buffered, so it is lost. It is logged (`durable_inbound`, status `rejected`),
  but not counted in `/health`. This can happen only to a late batch that the old socket
  emits after the bridge was already re-paired to another account. A batch caught by the
  reconnect during its own legacy media download is already spooled under its account.
- `delivery_count` restarts at zero when the bridge restarts.
- Unknown or intent ledger records never expire; reconciling them is manual or A2 work.
- A spool cannot be re-bound. Re-pairing the bridge to another account needs another
  spool (`WHATSAPP_DURABLE_SPOOL_DIR`) or an operator decision on the old one.
- Only the real (resolved) ancestor chain is fsynced. If the configured spool path goes
  through a symlink, the symlink's own directory entry is not fsynced; the bridge never
  creates symlinks, so such a symlink is an operator decision.
- Startup needs read permission on every directory of the real spool path up to `/` (to
  open it for fsync); otherwise it fails closed.
- Repeating the binding write after a failed fsync relies on the filesystem reporting a
  later fsync failure honestly. Nothing here is a power-loss guarantee beyond what
  fsync reports.
- **Debt (P2, not fixed by policy: only P0/P1 block).** From the canonical review
  `canonical-a1-system-ca`:
  - Startup checks the schema and filename id of each record, but not its other fields.
    A record with missing or invalid fields (for example, an inbound record without
    `state`, or a send record without `chunks`) loads; it may then never be claimable,
    or its receipt lookup may return 500.
  - A multi-chunk send checks the live account before each chunk, but not quarantine.
    If maintenance quarantines the spool while a send is in progress, the remaining
    chunks can still be sent.
  - From the canonical review `canonical-a1-durability-p1-final`: without
    `WHATSAPP_DURABLE_SPOOL_DIR`, the default spool comes from the bridge's own
    `HERMES_HOME`, not from the gateway's effective profile. That is part of the A2 config
    work below.
  - From the same review: a receipt GET goes through the account gate. On an unbound
    spool it therefore writes the binding, even when the lookup then returns 400 or 404.
  - From the canonical review `canonical-a1-binding-ingest-final` (owner: A2; registered in
    a1-p1-emitter, not fixed). That review raised the multi-chunk send after quarantine again
    (already listed above). It also raised two new P2s:
    - **Ack whose rename succeeds but whose directory fsync fails.** The route returns 500
      ("not acked, will replay") and the envelope stays pending in memory. But the ack record
      is already renamed into place, so after a restart the loaded tombstone suppresses the
      replay, contrary to the documented 500.
    - **HTTP body limit versus send limit.** The durable send accepts up to 65,536 JavaScript
      string units, but the shared `express.json()` parser rejects bodies over 100 KB before
      the handler runs. A valid request of 60,000 `é` therefore gets 413, while an ASCII
      request of the same length reaches the ledger.
  - The canonical review `canonical-a1-emitter-final` (owner: A2; registered in a1-p1-eager,
    not fixed) raised these items again, all of them already listed above:
    - P2: multi-chunk send after quarantine;
    - P2: ack rename succeeds but directory fsync fails;
    - P2: record-field validation;
    - P2: body limit versus send limit;
    - P2: default spool versus the effective profile;
    - P3: receipt GET binding an unbound spool.
  - P3, found in a1-p1-emitter (owner: legacy bridge, outside A1; not fixed). The legacy
    (non-durable) part of the upsert handler still reads the global `sock` for `botIds` and
    the self-chat check. After a reconnect to another account, a late batch of the old socket
    is filtered with the new account's ids. Durable groups never reach that code.

## Open items that block activation (A2)

These are not fixed in A1. No group may be activated, and the composite audit is not
clean, until they are closed:
- **P1 — gateway reuse/health.** `gateway/platforms/whatsapp.py` reuses an already
  running bridge based on connection status alone. It does not verify the bridge's
  effective durable configuration, and does not yet read `/health` → `durable.degraded`
  or `durable.quarantined`.
- **P2 — YAML config.** The durable settings are read only from the bridge's environment.
  They are not yet defined in the profile's `config.yaml` and passed to the Node
  subprocess.
