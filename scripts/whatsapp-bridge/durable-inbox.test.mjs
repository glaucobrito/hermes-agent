// UNIT / CONTRACT tests for the durable WhatsApp group transport (unit A1).
//
// The WhatsApp provider below is an explicit FAKE (records calls, returns
// Baileys-shaped results). These tests are NOT an integration test against
// WhatsApp. Filesystem (tmp dirs) and HTTP (node:http on 127.0.0.1) are real.
// All JIDs/IDs are fictitious.
//
// #atende:SPEC-licencas-acompanhamento/REQ-R4 REQ-R5 REQ-R8 REQ-R2
import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import http from 'node:http';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  chmodSync, existsSync, fsyncSync, linkSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync,
  statSync, symlinkSync, writeFileSync,
} from 'node:fs';

import * as durableModule from './durable-inbox.js';

const { DurableConfigError, createDurableTransport, parseDurableConfig, routeDurableRequest } = durableModule;
// Added in A1-followup2; read through the namespace so a missing export fails only its own tests.
const { DurableOwnershipError, LEGACY_MESSAGE_ROUTES, installDurableRoutes } = durableModule;
// Added in a1-p1-emitter (same reason).
const { captureInboundOrigin, routeUpsertBatch } = durableModule;
const MODULE_URL = new URL('./durable-inbox.js', import.meta.url).href;
const BRIDGE_PATH = fileURLToPath(new URL('./bridge.js', import.meta.url));

const GROUP_A = '120363000000000001@g.us';
const GROUP_B = '120363000000000002@g.us';
const GROUP_UNLISTED = '120363000000000099@g.us';
const DM_JID = '5500000000002@s.whatsapp.net';
const BOT_RAW = '5500000000001:7@s.whatsapp.net';
const BOT_CANONICAL = '5500000000001@s.whatsapp.net';
const MEMBER_LID = '100000000000001@lid';
const MEMBER_LID_2 = '100000000000002@lid';
// Another (fictitious) WhatsApp account re-paired onto the same bridge/spool.
const ACCOUNT_B_RAW = '5500000000003:2@s.whatsapp.net';
const ACCOUNT_B = '5500000000003@s.whatsapp.net';
// The emitting socket's origin, captured as routeUpsertBatch does in bridge.js. A direct ingest call below
// models a batch emitted by the live socket, so it passes that socket's origin.
const originOf = (raw) => captureInboundOrigin({ user: { id: raw } });
const BOT_ORIGIN = originOf(BOT_RAW);
const HEX64 = /^[0-9a-f]{64}$/;
const DAY_MS = 86_400_000;
const CAN_DROP_PERMS = typeof process.getuid === 'function' && process.getuid() !== 0;
const NO_PERMS_SKIP = CAN_DROP_PERMS ? false : 'running as root: directory permissions do not block writes';

function restorePerms(spool) {
  for (const sub of ['inbox', 'sends', 'tmp']) {
    const dir = path.join(spool, sub);
    if (existsSync(dir)) chmodSync(dir, 0o700);
  }
}

function tmpSpool(t) {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-durable-'));
  const spool = path.join(root, 'spool');
  t.after(() => {
    if (existsSync(spool)) restorePerms(spool);
    rmSync(root, { recursive: true, force: true });
  });
  return spool;
}

function config(spool, extraEnv = {}) {
  return parseDurableConfig({
    WHATSAPP_DURABLE_GROUP_IDS: JSON.stringify([GROUP_A, GROUP_B]),
    WHATSAPP_DURABLE_SPOOL_DIR: spool,
    ...extraEnv,
  }, { homeDir: '/nonexistent-home' });
}

// FAKE provider (UNIT/contract only): counts external calls.
function fakeProvider({ connected = true, onSend } = {}) {
  const provider = {
    calls: [],
    connected,
    sendMessage: async (jid, payload, options) => {
      provider.calls.push({ jid, payload, options });
      const n = provider.calls.length;
      if (onSend) return onSend(n, { jid, payload, options });
      return { key: { id: `FAKEOUT${n}`, remoteJid: jid, fromMe: true } };
    },
  };
  return provider;
}

// In-process stand-in for "the live bridge process that owns a spool". A restart
// simulation closes the previous owner first (closing its fd releases the kernel
// lock exactly like process death); the real two-process case is tested separately.
const OPEN_OWNERS = new Map();
afterEach(() => {
  for (const owner of OPEN_OWNERS.values()) owner.close();
  OPEN_OWNERS.clear();
});

function makeTransport(spool, {
  provider = fakeProvider(), clock = { t: 1_700_000_000_000 }, env = {}, chunker = (m) => [m], log, ...extraDeps
} = {}) {
  OPEN_OWNERS.get(spool)?.close();
  OPEN_OWNERS.delete(spool);
  const logs = [];
  const transport = createDurableTransport(config(spool, env), {
    now: () => clock.t,
    sendMessage: provider.sendMessage,
    isConnected: () => provider.connected,
    prepareChunks: chunker,
    getAccountJid: () => BOT_RAW,
    betweenChunks: async () => {},
    log: (entry) => { logs.push(entry); log?.(entry); },
    ...extraDeps,
  });
  OPEN_OWNERS.set(spool, transport);
  return { transport, provider, clock, logs };
}

// Technical timer stand-in (UNIT): captures the maintenance interval so a test can tick it.
function fakeTimers() {
  const timers = {
    handles: [],
    setInterval(fn, ms) {
      const handle = { fn, ms, unrefCalled: false, cleared: false, unref() { this.unrefCalled = true; return this; } };
      timers.handles.push(handle);
      return handle;
    },
    clearInterval(handle) { handle.cleared = true; },
  };
  return timers;
}

// Shaped like a real Baileys `messages.upsert` WAMessage. Explicitly passed
// `undefined` values are kept (no default substitution) so absent fields can be tested.
const MSG_DEFAULTS = {
  remoteJid: GROUP_A, id: '3EB0A1B2C3D4E5F60001', participant: MEMBER_LID, fromMe: false,
  pushName: 'Membro Teste', message: { conversation: 'Licença 123 vence em 10 dias' }, messageTimestamp: 1_700_000_000,
};
function baileysMsg(overrides = {}) {
  const f = { ...MSG_DEFAULTS, ...overrides };
  return {
    key: { remoteJid: f.remoteJid, fromMe: f.fromMe, id: f.id, participant: f.participant },
    message: f.message, pushName: f.pushName, messageTimestamp: f.messageTimestamp,
  };
}

// Exact name writeJsonAtomic gives a crash leftover: <64 hex>.json.<16 hex>.tmp, private 0600.
const tmpLeftoverName = (n = 0) => `${createHash('sha256').update(`leftover-${n}`).digest('hex')}.json.${String(n).padStart(16, '0')}.tmp`;
function writeTmpLeftover(spool, n = 0) {
  const file = path.join(spool, 'tmp', tmpLeftoverName(n));
  writeFileSync(file, 'partial', { mode: 0o600 });
  return file;
}
// Every persisted record file (dir, name, bytes) of the spool, to prove "no effect" (empty dirs add nothing).
const spoolSnapshot = (spool) => ['inbox', 'sends', 'account'].filter((d) => existsSync(path.join(spool, d)))
  .flatMap((d) => readdirSync(path.join(spool, d)).sort().map((f) => [d, f, readFileSync(path.join(spool, d, f), 'utf8')]));

const inboxFiles = (spool) => readdirSync(path.join(spool, 'inbox')).sort();
const sendFiles = (spool) => readdirSync(path.join(spool, 'sends')).sort();
const readJson = (file) => JSON.parse(readFileSync(file, 'utf8'));
const inboxRecord = (spool, envelopeId) => readJson(path.join(spool, 'inbox', `${envelopeId}.json`));
const onlySendRecord = (spool) => {
  const files = sendFiles(spool);
  assert.equal(files.length, 1);
  return readJson(path.join(spool, 'sends', files[0]));
};

async function startHttp(t, transport) {
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString('utf8');
    const url = new URL(req.url, 'http://127.0.0.1');
    const result = await routeDurableRequest(transport, {
      method: req.method, path: url.pathname, body: raw ? JSON.parse(raw) : {},
    }, () => {});
    // null => the bridge would fall through to its legacy express routes.
    const status = result ? result.status : 418;
    res.writeHead(status, { 'content-type': 'application/json' });
    res.end(JSON.stringify(result ? result.body : { legacy_fallthrough: true }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); }));
  const base = `http://127.0.0.1:${server.address().port}`;
  return async (method, urlPath, body) => {
    const res = await fetch(base + urlPath, {
      method,
      headers: { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: res.status, body: await res.json() };
  };
}

// ---------------------------------------------------------------- config

test('config: absent WHATSAPP_DURABLE_GROUP_IDS keeps legacy behaviour (disabled)', () => {
  assert.deepEqual(parseDurableConfig({ HOME: '/home/x' }, { homeDir: '/home/x' }), { enabled: false });
});

test('config: invalid values fail closed (throw), never silently ignored', () => {
  const groups = JSON.stringify([GROUP_A]);
  const bad = [
    { WHATSAPP_DURABLE_SPOOL_DIR: '/tmp/x' },
    { WHATSAPP_DURABLE_GROUP_IDS: '' },
    { WHATSAPP_DURABLE_GROUP_IDS: 'not-json' },
    { WHATSAPP_DURABLE_GROUP_IDS: JSON.stringify(GROUP_A) },
    { WHATSAPP_DURABLE_GROUP_IDS: '[]' },
    { WHATSAPP_DURABLE_GROUP_IDS: JSON.stringify([GROUP_A, GROUP_A]) },
    { WHATSAPP_DURABLE_GROUP_IDS: JSON.stringify([DM_JID]) },
    { WHATSAPP_DURABLE_GROUP_IDS: JSON.stringify(['../../etc@g.us']) },
    { WHATSAPP_DURABLE_GROUP_IDS: JSON.stringify([` ${GROUP_A}`]) },
    { WHATSAPP_DURABLE_GROUP_IDS: JSON.stringify([42]) },
    { WHATSAPP_DURABLE_GROUP_IDS: groups, WHATSAPP_DURABLE_SPOOL_DIR: 'relative/spool' },
    { WHATSAPP_DURABLE_GROUP_IDS: groups, WHATSAPP_DURABLE_LEASE_MS: '100' },
    { WHATSAPP_DURABLE_GROUP_IDS: groups, WHATSAPP_DURABLE_LEASE_MS: '12s' },
    { WHATSAPP_DURABLE_GROUP_IDS: groups, WHATSAPP_DURABLE_RETENTION_DAYS: '1' },
    { WHATSAPP_DURABLE_GROUP_IDS: groups, WHATSAPP_DURABLE_MAX_TEXT_CHARS: '0' },
    { WHATSAPP_DURABLE_GROUP_IDS: groups, WHATSAPP_DURABLE_GROUPS: '[]' },
  ];
  for (const env of bad) {
    assert.throws(() => parseDurableConfig(env, { homeDir: '/home/x' }), DurableConfigError, JSON.stringify(env));
  }
});

test('config: valid opt-in resolves canonical spool under HERMES_HOME, else ~/.hermes', () => {
  const groups = JSON.stringify([GROUP_A]);
  const c = parseDurableConfig({ WHATSAPP_DURABLE_GROUP_IDS: groups, HERMES_HOME: '/srv/hermes-home' }, { homeDir: '/home/x' });
  assert.equal(c.enabled, true);
  assert.deepEqual(c.groupJids, [GROUP_A]);
  assert.equal(c.spoolDir, '/srv/hermes-home/whatsapp/durable');
  assert.equal(c.leaseMs, 120_000);
  assert.equal(c.retentionMs, 35 * DAY_MS);
  assert.equal(c.maxTextChars, 8192);
  const d = parseDurableConfig({ WHATSAPP_DURABLE_GROUP_IDS: groups }, { homeDir: '/home/x' });
  assert.equal(d.spoolDir, '/home/x/.hermes/whatsapp/durable');
});

// ------------------------------------------------------------- isolation

test('isolation: only exact configured group JIDs are durable; DMs and other groups are not intercepted', (t) => {
  const spool = tmpSpool(t);
  const { transport } = makeTransport(spool);
  assert.equal(transport.isDurableGroup(GROUP_A), true);
  for (const jid of [GROUP_UNLISTED, GROUP_A.toUpperCase(), ` ${GROUP_A}`, `${GROUP_A}\n`, DM_JID, MEMBER_LID, undefined, null, 42]) {
    assert.equal(transport.isDurableGroup(jid), false, String(jid));
  }
  for (const remoteJid of [GROUP_UNLISTED, DM_JID]) {
    const r = transport.ingest(BOT_ORIGIN, baileysMsg({ remoteJid }));
    assert.deepEqual(r, { status: 'ignored', reason: 'not_durable_group' });
  }
  assert.deepEqual(inboxFiles(spool), []);
});

// ---------------------------------------------------------------- ingest

test('ingest: configured group message is fsync-persisted (0700 dirs / 0600 file) with real Baileys fields; LID preserved, nothing derived', (t) => {
  const spool = tmpSpool(t);
  const { transport, clock } = makeTransport(spool);
  const msg = baileysMsg({
    message: {
      extendedTextMessage: {
        text: 'Renovação\u0007 da licença\u0000 OK',
        contextInfo: { stanzaId: '3EB0QUOTED0001', participant: MEMBER_LID_2, quotedMessage: { conversation: 'original' } },
      },
    },
  });
  const r = transport.ingest(BOT_ORIGIN, msg);
  assert.equal(r.status, 'persisted');
  assert.match(r.envelopeId, HEX64);
  assert.deepEqual(inboxFiles(spool), [`${r.envelopeId}.json`]);
  for (const dir of [spool, path.join(spool, 'inbox'), path.join(spool, 'sends'), path.join(spool, 'tmp')]) {
    assert.equal(statSync(dir).mode & 0o777, 0o700, dir);
  }
  assert.equal(statSync(path.join(spool, 'inbox', `${r.envelopeId}.json`)).mode & 0o777, 0o600);
  assert.deepEqual(readdirSync(path.join(spool, 'tmp')), []);

  const rec = inboxRecord(spool, r.envelopeId);
  assert.deepEqual(Object.keys(rec).sort(), [
    'attachment', 'content_type', 'envelope_id', 'group_jid', 'instance_id', 'message_id', 'participant',
    'provider_timestamp', 'push_name', 'quoted', 'received_at', 'received_at_ms', 'schema', 'state', 'text',
    'text_original_chars', 'text_truncated',
  ]);
  assert.equal(rec.state, 'pending');
  assert.equal(rec.envelope_id, r.envelopeId);
  assert.equal(rec.instance_id, BOT_CANONICAL);
  assert.equal(rec.group_jid, GROUP_A);
  assert.equal(rec.message_id, '3EB0A1B2C3D4E5F60001');
  assert.equal(rec.participant, MEMBER_LID);
  assert.equal(rec.push_name, 'Membro Teste');
  assert.equal(rec.text, 'Renovação da licença OK');
  assert.equal(rec.content_type, 'text');
  assert.equal(rec.attachment, null);
  assert.deepEqual(rec.quoted, { stanza_id: '3EB0QUOTED0001', participant: MEMBER_LID_2 });
  assert.equal(rec.provider_timestamp, 1_700_000_000);
  assert.equal(rec.received_at, new Date(clock.t).toISOString());

  const noName = transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0NONAME0001', pushName: undefined }));
  assert.equal(inboxRecord(spool, noName.envelopeId).push_name, null);
});

test('ingest: attachments are metadata-only (no download, no media keys/urls) and text is bounded', (t) => {
  const spool = tmpSpool(t);
  const { transport } = makeTransport(spool, { env: { WHATSAPP_DURABLE_MAX_TEXT_CHARS: '256' } });
  const doc = transport.ingest(BOT_ORIGIN, baileysMsg({
    id: '3EB0DOC0001',
    message: {
      documentWithCaptionMessage: {
        message: {
          documentMessage: {
            url: 'https://media.example.invalid/x', mediaKey: 'c2VjcmV0LW1lZGlhLWtleQ==', directPath: '/v/t62/x',
            mimetype: 'application/pdf', fileName: '../../licenca.pdf', fileLength: 2048, caption: 'segue a licença',
          },
        },
      },
    },
  }));
  const rec = inboxRecord(spool, doc.envelopeId);
  assert.equal(rec.content_type, 'document');
  assert.equal(rec.text, 'segue a licença');
  assert.deepEqual(rec.attachment, {
    kind: 'document', mimetype: 'application/pdf', file_name: 'licenca.pdf', file_length: 2048, seconds: null, ptt: false, downloaded: false,
  });
  const raw = JSON.stringify(rec);
  for (const forbidden of ['mediaKey', 'c2VjcmV0', 'media.example.invalid', '/v/t62']) assert.ok(!raw.includes(forbidden), forbidden);

  const long = transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0LONG0001', message: { conversation: 'á'.repeat(300) } }));
  const longRec = inboxRecord(spool, long.envelopeId);
  assert.equal(Array.from(longRec.text).length, 256);
  assert.equal(longRec.text_truncated, true);
  assert.equal(longRec.text_original_chars, 300);
});

test('ingest: fromMe, undecryptable/empty, unsupported, invalid or traversal ids are never persisted', (t) => {
  const spool = tmpSpool(t);
  const { transport } = makeTransport(spool);
  assert.deepEqual(transport.ingest(BOT_ORIGIN, baileysMsg({ fromMe: true })), { status: 'ignored', reason: 'from_me' });
  assert.deepEqual(transport.ingest(BOT_ORIGIN, baileysMsg({ message: null })), { status: 'ignored', reason: 'no_message_content' });
  assert.deepEqual(
    transport.ingest(BOT_ORIGIN, baileysMsg({ message: { reactionMessage: { text: '👍', key: { id: 'X' } } } })),
    { status: 'ignored', reason: 'unsupported_content' },
  );
  for (const id of ['../../../etc/passwd', '', 'a/b', 'A'.repeat(129), undefined]) {
    assert.deepEqual(transport.ingest(BOT_ORIGIN, baileysMsg({ id })), { status: 'rejected', reason: 'invalid_message_id' }, String(id));
  }
  for (const participant of [undefined, '', '../x', `${MEMBER_LID} `]) {
    assert.deepEqual(transport.ingest(BOT_ORIGIN, baileysMsg({ participant })), { status: 'rejected', reason: 'invalid_participant' }, String(participant));
  }
  assert.deepEqual(inboxFiles(spool), []);
  // A placeholder (decrypt retry) must not block the real content that arrives later with the same id.
  const real = transport.ingest(BOT_ORIGIN, baileysMsg());
  assert.equal(real.status, 'persisted');
  assert.deepEqual(inboxFiles(spool), [`${real.envelopeId}.json`]);
  assert.deepEqual(readdirSync(path.dirname(spool)), ['spool']);
});

// ----------------------------------------------------- dedupe / lease / ack

test('dedupe: provider re-delivery is a duplicate before and after ack (tombstone prevents redelivery)', (t) => {
  const spool = tmpSpool(t);
  const { transport } = makeTransport(spool);
  const first = transport.ingest(BOT_ORIGIN, baileysMsg());
  assert.deepEqual(transport.ingest(BOT_ORIGIN, baileysMsg()), { status: 'duplicate', envelopeId: first.envelopeId });
  const claimed = transport.claim({});
  assert.equal(claimed.status, 200);
  assert.equal(claimed.body.leases.length, 1);
  const lease = claimed.body.leases[0];
  assert.equal(transport.ack({ envelope_id: lease.envelope_id, lease_token: lease.lease_token }).status, 200);
  assert.deepEqual(transport.ingest(BOT_ORIGIN, baileysMsg()), { status: 'duplicate', envelopeId: first.envelopeId });
  assert.equal(transport.claim({}).body.leases.length, 0);
  assert.deepEqual(inboxFiles(spool), [`${first.envelopeId}.json`]);
  const tomb = inboxRecord(spool, first.envelopeId);
  assert.equal(tomb.state, 'acked');
  assert.match(tomb.ack_token_sha256, HEX64);
  assert.ok(!JSON.stringify(tomb).includes(lease.lease_token), 'raw lease token must not be persisted');
});

test('lease: exclusive until expiry, then replay with a new token; ack needs envelope+valid token and is idempotent', (t) => {
  const spool = tmpSpool(t);
  const { transport, clock } = makeTransport(spool);
  const { envelopeId } = transport.ingest(BOT_ORIGIN, baileysMsg());
  const c1 = transport.claim({}).body.leases;
  assert.equal(c1.length, 1);
  assert.equal(c1[0].envelope_id, envelopeId);
  assert.match(c1[0].lease_token, HEX64);
  assert.equal(c1[0].delivery_count, 1);
  assert.equal(c1[0].envelope.participant, MEMBER_LID);
  assert.ok(!('state' in c1[0].envelope));
  assert.equal(transport.claim({}).body.leases.length, 0, 'leased envelope must not be handed out twice');

  clock.t += 120_000;
  const c3 = transport.claim({}).body.leases;
  assert.equal(c3.length, 1);
  assert.notEqual(c3[0].lease_token, c1[0].lease_token);
  assert.equal(c3[0].delivery_count, 2);
  assert.deepEqual(transport.ack({ envelope_id: envelopeId, lease_token: c1[0].lease_token }), { status: 409, body: { error: 'lease_invalid' } });
  assert.deepEqual(transport.ack({ envelope_id: envelopeId, lease_token: c3[0].lease_token }), { status: 200, body: { status: 'acked', envelope_id: envelopeId } });
  assert.deepEqual(transport.ack({ envelope_id: envelopeId, lease_token: c3[0].lease_token }), { status: 200, body: { status: 'already_acked', envelope_id: envelopeId } });
  assert.equal(transport.ack({ envelope_id: envelopeId, lease_token: c1[0].lease_token }).status, 409);
  assert.equal(transport.ack({ envelope_id: 'a'.repeat(64), lease_token: 'b'.repeat(64) }).status, 404);
  assert.equal(transport.ack({ envelope_id: '../x', lease_token: 'b'.repeat(64) }).status, 400);
  assert.equal(transport.ack({ envelope_id: envelopeId }).status, 400);
  assert.equal(transport.ack({ envelope_id: envelopeId, lease_token: c3[0].lease_token, extra: 1 }).status, 400);

  const second = transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0SECOND0001' }));
  const l2 = transport.claim({}).body.leases[0];
  clock.t += 120_000;
  assert.deepEqual(transport.ack({ envelope_id: second.envelopeId, lease_token: l2.lease_token }), { status: 409, body: { error: 'lease_expired' } });
  assert.equal(inboxRecord(spool, second.envelopeId).state, 'pending');
});

test('crash before ack: a new bridge process replays the pending envelope and rejects the stale token', (t) => {
  const spool = tmpSpool(t);
  const clock = { t: 1_700_000_000_000 };
  const a = makeTransport(spool, { clock }).transport;
  const { envelopeId } = a.ingest(BOT_ORIGIN, baileysMsg());
  const leaseA = a.claim({}).body.leases[0];

  const b = makeTransport(spool, { clock }).transport; // restart: process A died without ack
  const leaseB = b.claim({}).body.leases[0];
  assert.equal(leaseB.envelope_id, envelopeId);
  assert.notEqual(leaseB.lease_token, leaseA.lease_token);
  assert.deepEqual(leaseB.envelope, leaseA.envelope);
  assert.equal(b.ack({ envelope_id: envelopeId, lease_token: leaseA.lease_token }).status, 409);
  assert.equal(b.ack({ envelope_id: envelopeId, lease_token: leaseB.lease_token }).status, 200);

  const c = makeTransport(spool, { clock }).transport;
  assert.equal(c.claim({}).body.leases.length, 0);
  assert.equal(c.ingest(BOT_ORIGIN, baileysMsg()).status, 'duplicate');
});

test('persist failure: inbound is never claimable when the spool write fails; failed ack keeps it pending', { skip: NO_PERMS_SKIP }, async (t) => {
  const spool = tmpSpool(t);
  const { transport, clock } = makeTransport(spool);
  chmodSync(path.join(spool, 'inbox'), 0o500);
  const failed = transport.ingest(BOT_ORIGIN, baileysMsg());
  assert.equal(failed.status, 'persist_failed', 'a failed write is never reported as persisted');
  assert.equal(failed.code, 'EACCES');
  restorePerms(spool);
  assert.deepEqual(inboxFiles(spool), []);
  assert.deepEqual(readdirSync(path.join(spool, 'tmp')), []);
  assert.equal(transport.claim({}).body.leases.length, 0);

  const ok = transport.ingest(BOT_ORIGIN, baileysMsg());
  assert.equal(ok.status, 'persisted', 'failed attempt must not poison dedupe');
  const lease = transport.claim({}).body.leases[0];
  chmodSync(path.join(spool, 'inbox'), 0o500);
  const failedAck = await routeDurableRequest(transport, {
    method: 'POST', path: '/durable/inbox/ack', body: { envelope_id: lease.envelope_id, lease_token: lease.lease_token },
  }, () => {});
  restorePerms(spool);
  assert.equal(failedAck.status, 500);
  assert.equal(failedAck.body.error, 'durable_internal_error');
  assert.equal(inboxRecord(spool, ok.envelopeId).state, 'pending');
  clock.t += 120_000;
  assert.equal(transport.claim({}).body.leases[0].envelope_id, ok.envelopeId);
});

// ------------------------------------------------------------------ HTTP

test('http: claim serves only whitelisted groups; HTTP cannot configure groups nor inject trusted messages', async (t) => {
  const spool = tmpSpool(t);
  makeTransport(spool).transport.ingest(BOT_ORIGIN, baileysMsg({ remoteJid: GROUP_B, id: '3EB0GROUPB0001' }));
  const { transport } = makeTransport(spool, { env: { WHATSAPP_DURABLE_GROUP_IDS: JSON.stringify([GROUP_A]) } });
  const before = inboxFiles(spool);
  const call = await startHttp(t, transport);

  assert.deepEqual(await call('POST', '/durable/inbox/claim', {}), { status: 200, body: { leases: [] } });
  assert.equal(transport.stats().inbox_unconfigured_group, 1);
  assert.equal((await call('POST', '/durable/inbox/claim', { group_ids: [GROUP_B] })).status, 400);
  assert.equal((await call('POST', '/durable/inbox/claim', { limit: 0 })).status, 400);
  assert.equal((await call('POST', '/durable/inbox/claim', { limit: 51 })).status, 400);
  const forgedEnvelope = { group_jid: GROUP_A, message_id: '3EB0FORGED', participant: MEMBER_LID, text: 'forjada' };
  assert.equal((await call('POST', '/durable/inbox', forgedEnvelope)).status, 404);
  assert.equal((await call('POST', '/durable/inbox/claim', { envelope: forgedEnvelope })).status, 400);
  assert.equal((await call('POST', '/durable/inbox/ack', { envelope_id: 'a'.repeat(64), lease_token: 'b'.repeat(64) })).status, 404);
  assert.equal((await call('PUT', '/durable/inbox/ack', {})).status, 404);
  assert.equal((await call('GET', '/durable/send/receipt/..%2F..%2Fetc%2Fpasswd')).status, 400);
  assert.deepEqual(inboxFiles(spool), before);
  assert.deepEqual(await call('POST', '/durable/inbox/claim', {}), { status: 200, body: { leases: [] } });
  assert.deepEqual(await call('GET', '/messages'), { status: 418, body: { legacy_fallthrough: true } });

  const disabled = (req) => routeDurableRequest(null, req, () => {});
  assert.deepEqual(await disabled({ method: 'POST', path: '/durable/inbox/claim', body: {} }), { status: 404, body: { error: 'durable_transport_disabled' } });
  assert.deepEqual(await disabled({ method: 'POST', path: '/send', body: { chatId: GROUP_A, message: 'x', attempt_id: 'attempt-0001' } }), { status: 400, body: { error: 'durable_transport_disabled' } });
  assert.equal(await disabled({ method: 'POST', path: '/send', body: { chatId: GROUP_A, message: 'x' } }), null);
});

// ------------------------------------------------------------------- send

const ATTEMPT = 'attempt-0001-licencas';

test('send attempt: multi-chunk confirmed with all IDs; retry returns originals without resend; conflicting payload rejected', async (t) => {
  const spool = tmpSpool(t);
  const { transport, provider } = makeTransport(spool, { chunker: (m) => m.split('|') });
  const call = await startHttp(t, transport);

  const r1 = await call('POST', '/send', { chatId: GROUP_A, message: 'parte1|parte2|parte3', attempt_id: ATTEMPT });
  assert.equal(r1.status, 200);
  assert.deepEqual(r1.body, {
    success: true, attempt_id: ATTEMPT, state: 'confirmed', messageId: 'FAKEOUT3', messageIds: ['FAKEOUT1', 'FAKEOUT2', 'FAKEOUT3'], replayed: false,
  });
  assert.deepEqual(provider.calls.map((c) => [c.jid, c.payload.text, c.options]), [
    [GROUP_A, 'parte1', undefined], [GROUP_A, 'parte2', undefined], [GROUP_A, 'parte3', undefined],
  ]);
  const ledgerFile = path.join(spool, 'sends', sendFiles(spool)[0]);
  const rec = onlySendRecord(spool);
  assert.equal(rec.state, 'confirmed');
  assert.deepEqual(rec.chunks.map((c) => [c.state, c.message_id]), [['accepted', 'FAKEOUT1'], ['accepted', 'FAKEOUT2'], ['accepted', 'FAKEOUT3']]);
  assert.ok(!readFileSync(ledgerFile, 'utf8').includes('parte1'), 'ledger must not store message text');
  assert.equal(statSync(ledgerFile).mode & 0o777, 0o600);

  const r2 = await call('POST', '/send', { chatId: GROUP_A, message: 'parte1|parte2|parte3', attempt_id: ATTEMPT });
  assert.equal(r2.status, 200);
  assert.deepEqual(r2.body.messageIds, ['FAKEOUT1', 'FAKEOUT2', 'FAKEOUT3']);
  assert.equal(r2.body.replayed, true);
  const r3 = await call('POST', '/send', { chatId: GROUP_A, message: 'outro texto', attempt_id: ATTEMPT });
  assert.deepEqual(r3, { status: 409, body: { error: 'attempt_conflict', attempt_id: ATTEMPT } });
  assert.equal(provider.calls.length, 3);

  const before = readFileSync(ledgerFile, 'utf8');
  const receipt = await call('GET', `/durable/send/receipt/${ATTEMPT}`);
  assert.equal(receipt.status, 200);
  assert.equal(receipt.body.state, 'confirmed');
  assert.equal(receipt.body.chat_jid, GROUP_A);
  assert.deepEqual(receipt.body.message_ids, ['FAKEOUT1', 'FAKEOUT2', 'FAKEOUT3']);
  assert.equal(readFileSync(ledgerFile, 'utf8'), before, 'receipt is read-only');
  assert.equal((await call('GET', '/durable/send/receipt/attempt-unknown-0001')).status, 404);
  assert.equal((await call('GET', '/durable/send/receipt/x')).status, 400);
});

test('send attempt: legacy /send without attempt_id is untouched for unconfigured chats, denied for configured groups; attempt mode requires a configured group', async (t) => {
  const spool = tmpSpool(t);
  const { transport, provider } = makeTransport(spool);
  for (const chatId of [GROUP_UNLISTED, DM_JID]) {
    assert.equal(await routeDurableRequest(transport, { method: 'POST', path: '/send', body: { chatId, message: 'oi' } }, () => {}), null);
  }
  assert.deepEqual(
    await routeDurableRequest(transport, { method: 'POST', path: '/send', body: { chatId: GROUP_A, message: 'oi' } }, () => {}),
    { status: 403, body: { error: 'legacy_route_disabled_for_durable_group', route: '/send' } },
  );
  for (const chatId of [GROUP_UNLISTED, DM_JID]) {
    assert.deepEqual(await transport.send({ chatId, message: 'oi', attempt_id: ATTEMPT }), { status: 403, body: { error: 'chat_not_configured' } });
  }
  assert.equal((await transport.send({ chatId: GROUP_A, message: 'oi', attempt_id: 'x' })).status, 400);
  assert.equal((await transport.send({ chatId: GROUP_A, message: 'oi', attempt_id: ATTEMPT, mentions: [] })).status, 400);
  assert.equal((await transport.send({ chatId: GROUP_A, message: '', attempt_id: ATTEMPT })).status, 400);
  assert.equal(provider.calls.length, 0);
  assert.deepEqual(sendFiles(spool), []);
});

test('send attempt: provider error mid-chunks => delivery_unknown with accepted IDs, never resent', async (t) => {
  const spool = tmpSpool(t);
  const provider = fakeProvider({
    onSend: (n) => { if (n === 2) throw new Error('Connection Closed'); return { key: { id: `FAKEOUT${n}` } }; },
  });
  const { transport } = makeTransport(spool, { provider, chunker: (m) => m.split('|') });
  const body = { chatId: GROUP_A, message: 'a|b|c', attempt_id: ATTEMPT };
  const r1 = await transport.send(body);
  assert.equal(r1.status, 502);
  assert.deepEqual(r1.body, { success: false, error: 'delivery_unknown', state: 'delivery_unknown', attempt_id: ATTEMPT, messageIds: ['FAKEOUT1'] });
  const r2 = await transport.send(body);
  assert.equal(r2.status, 409);
  assert.equal(r2.body.error, 'delivery_unknown');
  assert.deepEqual(r2.body.messageIds, ['FAKEOUT1']);
  assert.equal(provider.calls.length, 2);
  const receipt = transport.receipt(ATTEMPT).body;
  assert.equal(receipt.state, 'delivery_unknown');
  assert.deepEqual(receipt.chunks.map((c) => [c.state, c.message_id]), [['accepted', 'FAKEOUT1'], ['pending', null], ['pending', null]]);
  assert.equal(onlySendRecord(spool).state, 'unknown');
});

test('send attempt: Baileys result without key.id is not acceptance evidence => delivery_unknown', async (t) => {
  const spool = tmpSpool(t);
  const provider = fakeProvider({ onSend: () => ({}) });
  const { transport } = makeTransport(spool, { provider });
  const body = { chatId: GROUP_A, message: 'oi', attempt_id: ATTEMPT };
  assert.equal((await transport.send(body)).status, 502);
  assert.equal((await transport.send(body)).status, 409);
  assert.equal(provider.calls.length, 1);
  assert.equal(onlySendRecord(spool).state, 'unknown');
});

test('send attempt: ledger intent persist failure aborts BEFORE any external call', { skip: NO_PERMS_SKIP }, async (t) => {
  const spool = tmpSpool(t);
  const { transport, provider } = makeTransport(spool);
  const body = { chatId: GROUP_A, message: 'oi', attempt_id: ATTEMPT };
  chmodSync(path.join(spool, 'sends'), 0o500);
  assert.deepEqual(await transport.send(body), { status: 500, body: { error: 'ledger_persist_failed', attempt_id: ATTEMPT } });
  restorePerms(spool);
  assert.equal(provider.calls.length, 0);
  assert.deepEqual(sendFiles(spool), []);
  assert.equal((await transport.send(body)).status, 200, 'nothing was sent, so a retry may send');
  assert.equal(provider.calls.length, 1);
});

test('send attempt: ledger loss after Baileys accept => delivery_unknown, and a restarted bridge never resends', { skip: NO_PERMS_SKIP }, async (t) => {
  const spool = tmpSpool(t);
  const clock = { t: 1_700_000_000_000 };
  const provider = fakeProvider({
    onSend: (n) => { chmodSync(path.join(spool, 'sends'), 0o500); return { key: { id: `FAKEOUT${n}` } }; },
  });
  const a = makeTransport(spool, { provider, clock }).transport;
  const body = { chatId: GROUP_A, message: 'oi', attempt_id: ATTEMPT };
  const r1 = await a.send(body);
  restorePerms(spool);
  assert.equal(r1.status, 502);
  assert.deepEqual(r1.body.messageIds, ['FAKEOUT1']);
  const onDisk = onlySendRecord(spool);
  assert.equal(onDisk.state, 'intent');
  assert.deepEqual(onDisk.chunks.map((c) => c.state), ['pending']);

  const providerB = fakeProvider();
  const b = makeTransport(spool, { provider: providerB, clock }).transport; // simulated crash/restart
  const r2 = await b.send(body);
  assert.equal(r2.status, 409);
  assert.equal(r2.body.error, 'delivery_unknown');
  assert.equal(providerB.calls.length, 0);
  assert.equal(b.receipt(ATTEMPT).body.state, 'delivery_unknown');
  assert.equal(b.receipt(ATTEMPT).body.ledger_state, 'intent');
});

test('send attempt: not connected => 503 with no ledger record; concurrent same attempt => attempt_in_progress', async (t) => {
  const spool = tmpSpool(t);
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const provider = fakeProvider({ connected: false, onSend: async (n) => { await gate; return { key: { id: `FAKEOUT${n}` } }; } });
  const { transport } = makeTransport(spool, { provider });
  const body = { chatId: GROUP_A, message: 'oi', attempt_id: ATTEMPT };
  assert.deepEqual(await transport.send(body), { status: 503, body: { error: 'not_connected', attempt_id: ATTEMPT } });
  assert.deepEqual(sendFiles(spool), []);

  provider.connected = true;
  const pending = transport.send(body);
  assert.deepEqual(await transport.send(body), { status: 409, body: { error: 'attempt_in_progress', attempt_id: ATTEMPT } });
  assert.equal(transport.receipt(ATTEMPT).body.state, 'in_progress');
  release();
  assert.equal((await pending).status, 200);
  assert.equal(provider.calls.length, 1);
  assert.equal(transport.receipt(ATTEMPT).body.state, 'confirmed');
});

test('send attempt: replyTo quotes the real inbound of the same group only, otherwise errors without sending', async (t) => {
  const spool = tmpSpool(t);
  const { transport, provider } = makeTransport(spool, { chunker: (m) => m.split('|') });
  transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0QA0001', message: { conversation: 'pergunta no grupo A' } }));
  transport.ingest(BOT_ORIGIN, baileysMsg({ remoteJid: GROUP_B, id: '3EB0QB0001' }));
  transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0QDOC01', message: { imageMessage: { mimetype: 'image/jpeg', caption: 'foto' } } }));

  const ok = await transport.send({ chatId: GROUP_A, message: 'r1|r2', attempt_id: 'attempt-reply-0001', replyTo: '3EB0QA0001' });
  assert.equal(ok.status, 200);
  assert.deepEqual(provider.calls[0].options, {
    quoted: { key: { remoteJid: GROUP_A, fromMe: false, id: '3EB0QA0001', participant: MEMBER_LID }, message: { conversation: 'pergunta no grupo A' } },
  });
  assert.equal(provider.calls[1].options, undefined, 'only the first chunk quotes');

  const cases = [
    ['3EB0QB0001', 422, 'reply_target_not_found'],
    ['3EB0NOPE01', 422, 'reply_target_not_found'],
    ['3EB0QDOC01', 422, 'reply_target_not_quotable'],
    ['../etc', 400, 'invalid_request'],
  ];
  for (const [replyTo, status, error] of cases) {
    const r = await transport.send({ chatId: GROUP_A, message: 'x', attempt_id: `attempt-reply-${replyTo.length}${status}`, replyTo });
    assert.equal(r.status, status, replyTo);
    assert.equal(r.body.error, error, replyTo);
  }
  assert.equal(provider.calls.length, 2);
  assert.equal(sendFiles(spool).length, 1);
});

// -------------------------------------------------------- retention/startup

test('retention: acked tombstones and confirmed ledger entries purge after retention; pending/unknown never purge', async (t) => {
  const spool = tmpSpool(t);
  const provider = fakeProvider({
    onSend: (n, { payload }) => { if (payload.text === 'boom') throw new Error('timeout'); return { key: { id: `FAKEOUT${n}` } }; },
  });
  const { transport, clock } = makeTransport(spool, { provider });
  const m1 = transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0RET0001' }));
  const m2 = transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0RET0002' }));
  const leases = transport.claim({}).body.leases;
  const l1 = leases.find((l) => l.envelope_id === m1.envelopeId);
  assert.equal(transport.ack({ envelope_id: l1.envelope_id, lease_token: l1.lease_token }).status, 200);
  assert.equal((await transport.send({ chatId: GROUP_A, message: 'ok', attempt_id: 'attempt-ret-confirmed' })).status, 200);
  assert.equal((await transport.send({ chatId: GROUP_A, message: 'boom', attempt_id: 'attempt-ret-unknown' })).status, 502);

  clock.t += 36 * DAY_MS;
  const replay = transport.claim({}).body.leases;
  assert.deepEqual(replay.map((l) => l.envelope_id), [m2.envelopeId]);
  assert.deepEqual(inboxFiles(spool), [`${m2.envelopeId}.json`]);
  assert.equal(onlySendRecord(spool).attempt_id, 'attempt-ret-unknown');
  assert.equal(transport.receipt('attempt-ret-confirmed').status, 404);
  assert.equal((await transport.send({ chatId: GROUP_A, message: 'boom', attempt_id: 'attempt-ret-unknown' })).status, 409);
});

test('startup fails closed on a tampered spool (unexpected entry, corrupt record, symlink); discards crash tmp files', (t) => {
  const spool = tmpSpool(t);
  makeTransport(spool).transport.ingest(BOT_ORIGIN, baileysMsg());
  const inbox = path.join(spool, 'inbox');

  writeFileSync(path.join(inbox, 'README'), 'x');
  assert.throws(() => makeTransport(spool), /unexpected entry/);
  rmSync(path.join(inbox, 'README'));

  writeFileSync(path.join(inbox, `${'c'.repeat(64)}.json`), '{not json');
  assert.throws(() => makeTransport(spool), /corrupt record/);
  rmSync(path.join(inbox, `${'c'.repeat(64)}.json`));

  symlinkSync('/etc/hostname', path.join(inbox, `${'d'.repeat(64)}.json`));
  assert.throws(() => makeTransport(spool), /not a regular file/);
  rmSync(path.join(inbox, `${'d'.repeat(64)}.json`));

  writeTmpLeftover(spool);
  const { logs } = makeTransport(spool);
  assert.deepEqual(readdirSync(path.join(spool, 'tmp')), []);
  assert.deepEqual(logs, [{ event: 'durable_spool_tmp_discarded', count: 1 }]);
});

// ======================================================= A1-followup2 (ownership, fsync, routing, retention)

function spawnOwner(t, spool, mode) {
  // A real second OS process owning the spool through the same module (no bridge, no WhatsApp).
  const script = [
    `import { createDurableTransport, parseDurableConfig } from ${JSON.stringify(MODULE_URL)};`,
    `const config = parseDurableConfig({ WHATSAPP_DURABLE_GROUP_IDS: ${JSON.stringify(JSON.stringify([GROUP_A]))}, WHATSAPP_DURABLE_SPOOL_DIR: process.env.OWNER_SPOOL }, { homeDir: '/nonexistent-home' });`,
    `createDurableTransport(config, { sendMessage: async () => { throw new Error('owner child never sends'); }, isConnected: () => false,`,
    `  prepareChunks: (m) => [m], getAccountJid: () => undefined, betweenChunks: async () => {}, log: () => {} });`,
    `process.stdout.write('owner-ready\\n');`,
    `if (process.env.OWNER_MODE === 'hold') process.stdin.resume();`,
  ].join('\n');
  const child = spawn(process.execPath, ['--input-type=module', '-e', script], {
    env: { PATH: process.env.PATH, OWNER_SPOOL: spool, OWNER_MODE: mode }, stdio: ['pipe', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', (d) => { stderr += d; });
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
  const ready = new Promise((resolve, reject) => {
    let out = '';
    child.stdout.on('data', (d) => { out += d; if (out.includes('owner-ready\n')) resolve(); });
    child.once('exit', (code, signal) => reject(new Error(`owner exited (${code ?? signal}) before ready: ${stderr}`)));
  });
  return { child, ready, stderr: () => stderr };
}

test('ownership: a second live process can neither clean nor load an owned spool; SIGKILL of the owner frees it (no stale-lock steal)', async (t) => {
  assert.equal(typeof DurableOwnershipError, 'function', 'DurableOwnershipError must be exported');
  const spool = tmpSpool(t);
  const owner = spawnOwner(t, spool, 'hold');
  await owner.ready;
  const leftover = writeTmpLeftover(spool);
  const corrupt = path.join(spool, 'inbox', `${'e'.repeat(64)}.json`);
  writeFileSync(corrupt, '{corrupt'); // loading would throw DurableStateError: ownership must fail first
  for (let i = 0; i < 2; i += 1) {
    assert.throws(() => makeTransport(spool),
      (err) => err instanceof DurableOwnershipError && /another live bridge process/.test(err.message));
  }
  assert.ok(existsSync(leftover), 'a contender must not clean the owner\'s spool');
  rmSync(corrupt);
  const exited = once(owner.child, 'exit');
  owner.child.kill('SIGKILL');
  const [, signal] = await exited;
  assert.equal(signal, 'SIGKILL');
  const { logs } = makeTransport(spool); // the kernel released the lock with the process
  assert.equal(existsSync(leftover), false);
  assert.deepEqual(logs, [{ event: 'durable_spool_tmp_discarded', count: 1 }]);
});

test('lifecycle: the maintenance timer is unref\'d, so an idle owner process exits on its own and releases the lock', async (t) => {
  const spool = tmpSpool(t);
  const owner = spawnOwner(t, spool, 'idle');
  const exited = once(owner.child, 'exit');
  await owner.ready;
  const [code, signal] = await exited;
  assert.deepEqual([code, signal], [0, null], owner.stderr());
  makeTransport(spool);
});

test('first provision: each new ancestor is created 0700 and fsynced into its parent before traffic; existing parents untouched', (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-provision-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  chmodSync(root, 0o755);
  const a = path.join(root, 'a');
  const b = path.join(a, 'b');
  const spool = path.join(b, 'spool');
  const synced = [];
  const { transport } = makeTransport(spool, { fsync: (fd, target) => { synced.push(target); fsyncSync(fd); } });
  assert.equal(statSync(root).mode & 0o777, 0o755, 'pre-existing parent permissions are never modified');
  for (const dir of [a, b, spool, ...['inbox', 'sends', 'tmp'].map((d) => path.join(spool, d))]) {
    assert.equal(statSync(dir).mode & 0o777, 0o700, dir);
  }
  assert.equal(statSync(path.join(spool, 'owner.lock')).mode & 0o777, 0o600);
  const order = [root, a, b].map((dir) => synced.indexOf(dir));
  assert.ok(order.every((i) => i >= 0), `every parent of a new dir is fsynced: ${JSON.stringify(synced)}`);
  assert.deepEqual([...order].sort((x, y) => x - y), order, 'parents are fsynced in creation order');
  assert.ok(synced.includes(spool), 'new spool entries (inbox/sends/tmp/lock) are fsynced');
  const before = synced.length;
  assert.equal(transport.ingest(BOT_ORIGIN, baileysMsg()).status, 'persisted');
  assert.ok(synced.slice(before).includes(path.join(spool, 'inbox')));
});

test('first provision: an injected fsync failure (UNIT fault) aborts startup before ownership, clean or load, and releases the lock', (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-provision-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const a = path.join(root, 'a');
  const spool = path.join(a, 'spool');
  const failingOn = (target) => (fd, p) => {
    if (p === target) throw Object.assign(new Error('EIO injected'), { code: 'EIO' });
    fsyncSync(fd);
  };
  assert.throws(() => makeTransport(spool, { fsync: failingOn(a) }), /EIO injected/);
  assert.equal(existsSync(path.join(spool, 'owner.lock')), false, 'never reached ownership');

  makeTransport(spool).transport.ingest(BOT_ORIGIN, baileysMsg());
  const leftover = writeTmpLeftover(spool);
  assert.throws(() => makeTransport(spool, { fsync: failingOn(path.join(spool, 'inbox')) }), /EIO injected/);
  assert.ok(existsSync(leftover), 'nothing cleaned after a startup fsync failure');
  const { transport } = makeTransport(spool); // lock was released by the failed startup
  assert.equal(existsSync(leftover), false);
  assert.equal(transport.claim({}).body.leases.length, 1);
});

async function loadExpress() {
  const req = createRequire(import.meta.url);
  try {
    return { express: (await import('express')).default, version: req('express/package.json').version, source: 'node_modules' };
  } catch { /* express not installed in this worktree */ }
  const dir = process.env.HERMES_BRIDGE_TEST_EXPRESS_DIR;
  if (!dir) return null;
  return { express: req(dir), version: req(path.join(dir, 'package.json')).version, source: dir };
}
const EXPRESS = await loadExpress();

async function listenApp(t, app) {
  const server = http.createServer(app);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); }));
  const base = `http://127.0.0.1:${server.address().port}`;
  return async (urlPath, body) => {
    const res = await fetch(base + urlPath, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    const text = await res.text();
    let parsed;
    try { parsed = JSON.parse(text); } catch { parsed = { raw: text }; }
    return { status: res.status, body: parsed };
  };
}

test('express wiring (real Express + real HTTP): every path variant Express routes to legacy /send, /edit, /send-media is guarded',
  { skip: EXPRESS ? false : 'express not resolvable (no node_modules; set HERMES_BRIDGE_TEST_EXPRESS_DIR)' }, async (t) => {
    assert.match(EXPRESS.version, /^4\./, 'bridge depends on express ^4');
    assert.equal(typeof installDurableRoutes, 'function', 'installDurableRoutes must be exported');
    const express = EXPRESS.express;
    const spool = tmpSpool(t);
    const provider = fakeProvider({
      onSend: (n, { payload }) => {
        if (payload.text === 'boom') throw new Error('socket closed');
        return { key: { id: `FAKEOUT${n}`, remoteJid: GROUP_A, fromMe: true } };
      },
    });
    const { transport } = makeTransport(spool, { provider, chunker: (m) => m.split('|') });
    const app = express();
    app.use(express.json());
    installDurableRoutes(app, transport, () => {});
    // Stand-ins for bridge.js legacy handlers (they need a live Baileys socket), registered AFTER the guards as in bridge.js.
    const legacyHits = [];
    for (const route of ['/send', '/edit', '/send-media', '/typing']) {
      app.post(route, (req, res) => { legacyHits.push({ route, path: req.path }); res.json({ legacy: route }); });
    }
    const post = await listenApp(t, app);

    const SEND_VARIANTS = ['/send', '/send/', '/SEND', '/Send/', '/sEnD?x=1', '/send/?attempt_id=q'];
    for (const v of SEND_VARIANTS) {
      const r = await post(v, { chatId: GROUP_A, message: 'tarefa velha' });
      assert.deepEqual(r, { status: 403, body: { error: 'legacy_route_disabled_for_durable_group', route: '/send' } }, v);
    }
    for (const chatId of [` ${GROUP_A} `, GROUP_A.toUpperCase(), GROUP_A.replace('@', ':3@'), [GROUP_A]]) {
      assert.equal((await post('/send/', { chatId, message: 'x' })).status, 403, JSON.stringify(chatId));
    }
    for (const v of ['/edit', '/edit/', '/EDIT', '/send-media', '/Send-Media/', '/SEND-MEDIA?x=1']) {
      const r = await post(v, { chatId: GROUP_A, messageId: '3EB0OLD0001', message: 'x', filePath: '/tmp/x.png' });
      assert.equal(r.status, 403, v);
      assert.equal(r.body.error, 'legacy_route_disabled_for_durable_group', v);
    }
    assert.deepEqual(legacyHits, []);
    assert.equal(provider.calls.length, 0);

    for (const v of [...SEND_VARIANTS, '/edit/', '/SEND-MEDIA']) {
      for (const chatId of [GROUP_UNLISTED, DM_JID]) {
        assert.equal((await post(v, { chatId, message: 'oi', messageId: '3EB0X', filePath: '/tmp/x' })).status, 200, `${v} ${chatId}`);
      }
    }
    assert.equal((await post('/typing', { chatId: GROUP_A })).status, 200, 'presence produces no message');
    assert.equal(legacyHits.length, (SEND_VARIANTS.length + 2) * 2 + 1);
    legacyHits.length = 0;

    transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0QUOTE0001', message: { conversation: 'pergunta' } }));
    const sent = await post('/SEND/', { chatId: GROUP_A, message: 'a|b|c', replyTo: '3EB0QUOTE0001', attempt_id: 'attempt-express-0001' });
    assert.equal(sent.status, 200);
    assert.deepEqual(sent.body.messageIds, ['FAKEOUT1', 'FAKEOUT2', 'FAKEOUT3']);
    assert.equal(provider.calls[0].options.quoted.key.id, '3EB0QUOTE0001');
    assert.equal(provider.calls[1].options, undefined);
    const replay = await post('/send', { chatId: GROUP_A, message: 'a|b|c', replyTo: '3EB0QUOTE0001', attempt_id: 'attempt-express-0001' });
    assert.equal(replay.status, 200);
    assert.equal(replay.body.replayed, true);
    assert.equal(provider.calls.length, 3);

    const unknown = await post('/Send/', { chatId: GROUP_A, message: 'ok|boom', attempt_id: 'attempt-express-0002' });
    assert.equal(unknown.status, 502);
    assert.deepEqual(unknown.body.messageIds, ['FAKEOUT4']);
    const unknownReplay = await post('/SEND?retry=1', { chatId: GROUP_A, message: 'ok|boom', attempt_id: 'attempt-express-0002' });
    assert.equal(unknownReplay.status, 409);
    assert.equal(unknownReplay.body.error, 'delivery_unknown');
    assert.equal(provider.calls.length, 5, 'uncertain attempt is never resent');

    assert.deepEqual(await post('/send/', { chatId: GROUP_UNLISTED, message: 'x', attempt_id: 'attempt-express-0003' }),
      { status: 403, body: { error: 'chat_not_configured' } });
    assert.equal((await post('/EDIT/', { chatId: GROUP_UNLISTED, messageId: '3EB0X', message: 'x', attempt_id: 'attempt-express-0004' })).status, 400);
    assert.equal((await post('/DURABLE/inbox/claim/', {})).status, 200);
    assert.deepEqual(legacyHits, []);
    assert.equal(provider.calls.length, 5);

    // Proof that the exported non-Express classifier equals Express's own matcher on a hostile corpus.
    const probe = express();
    probe.use(express.json());
    for (const route of LEGACY_MESSAGE_ROUTES) probe.post(route, (req, res) => res.json({ hit: route }));
    const probePost = await listenApp(t, probe);
    const corpus = ['/send', '/send/', '/SEND', '/Send/', '/send//', '/send/x', '/sendx', '/xsend', '/%73end', '/send%2F',
      '/send.json', '/edit', '/EDIT/', '/edit/x', '/send-media', '/SEND-MEDIA/', '/send-medi', '/send_media', '/send-media//'];
    for (const p of corpus) {
      const viaExpress = (await probePost(p, {})).body.hit ?? null;
      const classified = await routeDurableRequest(transport, { method: 'POST', path: p, body: { chatId: GROUP_A, message: 'x' } }, () => {});
      assert.equal(classified?.body?.route ?? null, viaExpress, p);
    }
  });

test('bridge.js wiring: guards installed before every legacy message route; every mutating route is classified', () => {
  const src = readFileSync(BRIDGE_PATH, 'utf8');
  const install = src.indexOf('installDurableRoutes(app, durable');
  assert.ok(install > 0, 'bridge.js must call installDurableRoutes(app, durable)');
  assert.ok(Array.isArray(LEGACY_MESSAGE_ROUTES), 'LEGACY_MESSAGE_ROUTES must be exported');
  const HARMLESS = new Set(['/typing']); // presence only: produces no message
  const routes = [...src.matchAll(/app\.(post|put|patch|delete|all)\(\s*'([^']+)'/g)];
  assert.ok(routes.length >= 4);
  for (const m of routes) {
    assert.ok(LEGACY_MESSAGE_ROUTES.includes(m[2]) || HARMLESS.has(m[2]), `unclassified mutating route ${m[2]}`);
    if (LEGACY_MESSAGE_ROUTES.includes(m[2])) assert.ok(m.index > install, `${m[2]} registered before its guard`);
  }
  assert.equal(/routeDurableRequest/.test(src), false, 'the old exact-path middleware is gone');
  // The upsert loop routes through routeUpsertBatch with the socket its listener belongs to (captured right after
  // makeWASocket, before the listeners), with no await before the call; the old inline branch is gone.
  const created = src.indexOf('sock = makeWASocket(');
  const emitter = src.indexOf('const emitter = sock;');
  const listener = src.indexOf("sock.ev.on('messages.upsert'");
  const routed = src.indexOf('for (const msg of routeUpsertBatch(durable, emitter, messages, logDurableInbound)) {');
  assert.ok(created > 0 && created < emitter && emitter < listener && listener < routed, 'emitter captured before the upsert listener');
  assert.equal(/\bawait\b/.test(src.slice(listener, routed).replace(/\/\/.*$/gm, '')), false, 'no await before the batch origin is captured');
  assert.equal(/durable\.(isDurableGroup|ingest)\(|handleDurableInbound/.test(src), false, 'no inline durable branch reading the global sock');
});

test('retention: a periodic sweep on the event loop purges acked bodies and confirmed receipts with the consumer down; unknown/intent/pending kept', async (t) => {
  const spool = tmpSpool(t);
  let onPurged;
  const purgedP = new Promise((resolve, reject) => {
    // Bounded deadline; also the only ref'd handle, because the maintenance timer is
    // unref'd by design and must not keep a process (or this test) alive by itself.
    const deadline = setTimeout(() => reject(new Error('no periodic purge within 5s')), 5_000);
    onPurged = (entry) => { clearTimeout(deadline); resolve(entry); };
  });
  const provider = fakeProvider({
    onSend: (n, { payload }) => {
      if (payload.text === 'boom') throw new Error('timeout');
      if (payload.text === 'hang') return new Promise(() => {});
      return { key: { id: `FAKEOUT${n}` } };
    },
  });
  const { transport, clock } = makeTransport(spool, {
    provider, maintenanceIntervalMs: 5, log: (e) => { if (e.event === 'durable_retention_purged') onPurged(e); },
  });
  const m1 = transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0PER0001' }));
  const m2 = transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0PER0002' }));
  const l1 = transport.claim({}).body.leases.find((l) => l.envelope_id === m1.envelopeId);
  assert.equal(transport.ack({ envelope_id: l1.envelope_id, lease_token: l1.lease_token }).status, 200);
  assert.equal((await transport.send({ chatId: GROUP_A, message: 'ok', attempt_id: 'attempt-per-confirmed' })).status, 200);
  assert.equal((await transport.send({ chatId: GROUP_A, message: 'boom', attempt_id: 'attempt-per-unknown' })).status, 502);
  transport.send({ chatId: GROUP_A, message: 'hang', attempt_id: 'attempt-per-intent' }); // never settles
  clock.t += 36 * DAY_MS; // no claim/startup after this point: only the timer can purge
  assert.deepEqual(await purgedP, { event: 'durable_retention_purged', inbox: 1, sends: 1 });
  assert.deepEqual(inboxFiles(spool), [`${m2.envelopeId}.json`]);
  assert.equal(transport.receipt('attempt-per-confirmed').status, 404);
  assert.equal(transport.receipt('attempt-per-unknown').body.state, 'delivery_unknown');
  assert.equal(transport.receipt('attempt-per-intent').body.ledger_state, 'intent');
  assert.equal(sendFiles(spool).length, 2);
});

test('maintenance: technical unref\'d interval cleared by close(); a sweep that finds tampered state quarantines the spool (fail closed)', async (t) => {
  const spool = tmpSpool(t);
  const timers = fakeTimers();
  const { transport, provider, logs } = makeTransport(spool, { timers });
  assert.equal(timers.handles.length, 1);
  const [handle] = timers.handles;
  assert.equal(handle.unrefCalled, true);
  assert.equal(handle.ms, 60_000);
  transport.ingest(BOT_ORIGIN, baileysMsg());
  handle.fn();
  assert.equal(transport.stats().degraded, false);
  assert.equal(transport.stats().quarantined, false);

  writeFileSync(path.join(spool, 'inbox', 'README'), 'x');
  handle.fn();
  assert.equal(transport.stats().quarantined, true);
  assert.equal(transport.stats().degraded, true);
  assert.deepEqual(logs.at(-1), { event: 'durable_spool_quarantined', code: 'DurableStateError' });
  const before = inboxFiles(spool);
  assert.equal(transport.claim({}).status, 503);
  assert.equal(transport.ack({ envelope_id: 'a'.repeat(64), lease_token: 'b'.repeat(64) }).status, 503);
  assert.equal((await transport.send({ chatId: GROUP_A, message: 'x', attempt_id: ATTEMPT })).status, 503);
  const r = transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0QUAR0001' }));
  assert.equal(r.status, 'persist_failed');
  assert.equal(r.retained, false);
  assert.deepEqual(inboxFiles(spool), before);
  assert.equal((await routeDurableRequest(transport, { method: 'POST', path: '/send', body: { chatId: GROUP_A, message: 'x' } }, () => {})).status, 403,
    'no legacy fallback while quarantined');
  assert.equal(provider.calls.length, 0);
  rmSync(path.join(spool, 'inbox', 'README'));
  transport.close();
  transport.close();
  assert.equal(handle.cleared, true);
});

test('ingest persist failure: never reported durable; bounded in-memory retry on the maintenance tick recovers or drops loudly; logs sanitized',
  { skip: NO_PERMS_SKIP }, (t) => {
    const spool = tmpSpool(t);
    const timers = fakeTimers();
    const { transport, logs } = makeTransport(spool, { timers });
    const [handle] = timers.handles;
    const inbox = path.join(spool, 'inbox');
    chmodSync(inbox, 0o500);
    const r = transport.ingest(BOT_ORIGIN, baileysMsg());
    assert.deepEqual([r.status, r.retained, r.code], ['persist_failed', true, 'EACCES']);
    assert.equal(transport.claim({}).body.leases.length, 0);
    assert.equal(transport.stats().degraded, true);
    assert.equal(transport.stats().ingest_retry_buffered, 1);
    handle.fn();
    assert.equal(logs.at(-1).status, 'persist_failed');
    restorePerms(spool);
    handle.fn();
    assert.equal(logs.at(-1).event, 'durable_inbound_retry');
    assert.equal(logs.at(-1).status, 'persisted');
    assert.equal(transport.claim({}).body.leases.length, 1);
    assert.equal(transport.stats().degraded, false);
    assert.equal(transport.stats().ingest_recovered, 1);

    chmodSync(inbox, 0o500);
    assert.equal(transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0DROP0001' })).retained, true);
    for (let i = 0; i < 4; i += 1) handle.fn();
    assert.equal(transport.stats().ingest_retry_buffered, 0);
    assert.equal(transport.stats().ingest_dropped, 1);
    assert.equal(transport.stats().degraded, true, 'a dropped inbound keeps health degraded until restart');
    assert.equal(logs.at(-1).retained, false);
    const flood = Array.from({ length: 65 }, (_, i) => transport.ingest(BOT_ORIGIN, baileysMsg({ id: `3EB0FLOOD${String(i).padStart(4, '0')}` })));
    assert.equal(flood.filter((x) => x.retained).length, 64, 'retry buffer is bounded');
    assert.equal(flood.at(-1).retained, false);
    restorePerms(spool);

    const dump = JSON.stringify([logs, transport.stats(), flood]);
    for (const secret of [GROUP_A, MEMBER_LID, BOT_CANONICAL, '3EB0A1B2C3D4E5F60001', 'Licença', spool]) {
      assert.equal(dump.includes(secret), false, `diagnostics leak ${secret}`);
    }
  });

// ======================================================= P1-final (canonical-final-a1: tmp cleanup, account binding)

test('startup tmp cleanup (P1a): every tmp/ entry must be an exact private writeJsonAtomic leftover before any is deleted; one foreign entry fails closed and nothing is cleaned', (t) => {
  const spool = tmpSpool(t);
  makeTransport(spool).transport.ingest(BOT_ORIGIN, baileysMsg());
  const tmp = path.join(spool, 'tmp');
  const outside = path.join(path.dirname(spool), 'outside-target.txt');
  writeFileSync(outside, 'foreign data', { mode: 0o600 });
  const hex = 'a'.repeat(64);
  const zero16 = '0'.repeat(16);
  const foreign = [
    ['leftover.tmp', 'file'], ['notes.txt', 'file'], [`${hex}.json.${zero16}.tmp.bak`, 'file'],
    [`${hex.toUpperCase()}.json.${zero16}.tmp`, 'file'], [`${hex}.json.${'0'.repeat(15)}.tmp`, 'file'],
    [`${'a'.repeat(63)}.json.${zero16}.tmp`, 'file'], [`${hex}.${zero16}.tmp`, 'file'], [`${hex}.json.${'g'.repeat(16)}.tmp`, 'file'],
    [tmpLeftoverName(900), 'public'], // exact name, but 0644
    [tmpLeftoverName(901), 'symlink'], // exact name, symlink to a file outside the spool
    [tmpLeftoverName(902), 'dir'],
    [tmpLeftoverName(903), 'hardlink'], // exact name, private, but a second link to a foreign file
  ];
  for (const [name, kind] of foreign) {
    const valid = [writeTmpLeftover(spool, 1)];
    const target = path.join(tmp, name);
    if (kind === 'symlink') symlinkSync(outside, target);
    else if (kind === 'dir') mkdirSync(target, { mode: 0o700 });
    else if (kind === 'hardlink') linkSync(outside, target);
    else {
      writeFileSync(target, 'foreign', { mode: 0o600 });
      if (kind === 'public') chmodSync(target, 0o644);
    }
    for (let n = 2; readdirSync(tmp)[0] === name && n < 66; n += 1) valid.push(writeTmpLeftover(spool, n));
    assert.notEqual(readdirSync(tmp)[0], name, 'fixture: the foreign entry comes after a valid leftover in readdir order');
    assert.throws(() => makeTransport(spool),
      (err) => err.name === 'DurableStateError' && /unexpected entry in spool tmp/.test(err.message), name);
    for (const file of valid) assert.ok(existsSync(file), `${name}: no leftover is deleted while another tmp entry is foreign`);
    assert.ok(lstatSync(target), `${name}: the foreign entry survives`);
    assert.equal(readFileSync(outside, 'utf8'), 'foreign data');
    rmSync(target, { recursive: true });
    for (const file of valid) rmSync(file);
  }
  for (const n of [0, 1, 2]) writeTmpLeftover(spool, n);
  const { transport, logs } = makeTransport(spool);
  assert.deepEqual(readdirSync(tmp), []);
  assert.deepEqual(logs, [{ event: 'durable_spool_tmp_discarded', count: 3 }]);
  assert.equal(transport.claim({}).body.leases.length, 1);
});

const ACCOUNT_DENIED = { status: 409, body: { error: 'account_mismatch' } };
const ACCOUNT_UNKNOWN = { status: 503, body: { error: 'account_unknown' } };

test('account binding (P1b): a switch to another account in the same group during execution is denied without exposure, effect or provider call; the bound account keeps working', async (t) => {
  const spool = tmpSpool(t);
  const account = { raw: BOT_RAW };
  const { transport, provider, clock } = makeTransport(spool, { getAccountJid: () => account.raw });
  const call = await startHttp(t, transport);
  const m1 = transport.ingest(BOT_ORIGIN, baileysMsg());
  assert.equal(m1.status, 'persisted');
  assert.equal((await call('POST', '/send', { chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT })).status, 200);
  const [lease] = (await call('POST', '/durable/inbox/claim', {})).body.leases;
  const m2 = transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0PEND0002' }));
  assert.equal(m2.status, 'persisted');
  const before = spoolSnapshot(spool);
  const calls = provider.calls.length;

  account.raw = ACCOUNT_B_RAW; // re-paired to another WhatsApp account, same configured group
  assert.deepEqual(await call('POST', '/durable/inbox/claim', {}), ACCOUNT_DENIED);
  clock.t += 120_000; // even after the lease expired nothing is re-served to the other account
  assert.deepEqual(await call('POST', '/durable/inbox/claim', {}), ACCOUNT_DENIED);
  assert.deepEqual(await call('POST', '/durable/inbox/ack', { envelope_id: lease.envelope_id, lease_token: lease.lease_token }), ACCOUNT_DENIED);
  assert.deepEqual(await call('POST', '/send', { chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT }), ACCOUNT_DENIED);
  assert.deepEqual(await call('POST', '/send', { chatId: GROUP_A, message: 'nova', attempt_id: 'attempt-0002-licencas' }), ACCOUNT_DENIED);
  assert.deepEqual(await call('GET', `/durable/send/receipt/${ATTEMPT}`), ACCOUNT_DENIED);
  assert.deepEqual(transport.ingest(originOf(ACCOUNT_B_RAW), baileysMsg({ id: '3EB0OTHER0001' })), { status: 'rejected', reason: 'account_mismatch' });
  assert.equal(transport.stats().account_mismatch, true);
  assert.equal(transport.stats().degraded, true);

  account.raw = undefined; // Baileys reports no account (disconnected / not paired)
  assert.deepEqual(await call('POST', '/durable/inbox/claim', {}), ACCOUNT_UNKNOWN);
  assert.deepEqual(await call('POST', '/durable/inbox/ack', { envelope_id: lease.envelope_id, lease_token: lease.lease_token }), ACCOUNT_UNKNOWN);
  assert.deepEqual(await call('POST', '/send', { chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT }), ACCOUNT_UNKNOWN);
  assert.deepEqual(await call('GET', `/durable/send/receipt/${ATTEMPT}`), ACCOUNT_UNKNOWN);
  assert.deepEqual(transport.ingest(originOf(undefined), baileysMsg({ id: '3EB0OTHER0002' })), { status: 'rejected', reason: 'account_unknown' });
  assert.equal(provider.calls.length, calls, 'no provider call for a denied account');
  assert.deepEqual(spoolSnapshot(spool), before, 'no spool effect for a denied account');

  account.raw = BOT_RAW.replace(':7@', ':9@'); // the bound account (another device suffix) resumes
  assert.equal(transport.stats().account_mismatch, false);
  const leases = (await call('POST', '/durable/inbox/claim', {})).body.leases;
  assert.deepEqual(leases.map((l) => l.envelope_id).sort(), [m1.envelopeId, m2.envelopeId].sort());
  for (const l of leases) {
    assert.equal((await call('POST', '/durable/inbox/ack', { envelope_id: l.envelope_id, lease_token: l.lease_token })).status, 200);
  }
  const replay = await call('POST', '/send', { chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT });
  assert.deepEqual([replay.status, replay.body.replayed, replay.body.messageIds], [200, true, ['FAKEOUT1']]);
  assert.equal((await call('GET', `/durable/send/receipt/${ATTEMPT}`)).body.state, 'confirmed');
  assert.equal(provider.calls.length, calls);
});

test('account binding (P1b): an account switch between chunks of a send stops before the next provider call (delivery_unknown, never resent)', async (t) => {
  const spool = tmpSpool(t);
  const account = { raw: BOT_RAW };
  const provider = fakeProvider({ onSend: (n) => { account.raw = ACCOUNT_B_RAW; return { key: { id: `FAKEOUT${n}` } }; } });
  const { transport } = makeTransport(spool, { provider, getAccountJid: () => account.raw, chunker: (m) => m.split('|') });
  const body = { chatId: GROUP_A, message: 'a|b|c', attempt_id: ATTEMPT };
  assert.deepEqual(await transport.send(body), {
    status: 502, body: { success: false, error: 'delivery_unknown', state: 'delivery_unknown', attempt_id: ATTEMPT, messageIds: ['FAKEOUT1'] },
  });
  assert.equal(provider.calls.length, 1);
  account.raw = BOT_RAW;
  const receipt = transport.receipt(ATTEMPT).body;
  assert.equal(receipt.error, 'account_changed');
  assert.deepEqual(receipt.chunks.map((c) => c.state), ['accepted', 'pending', 'pending']);
  assert.equal(onlySendRecord(spool).state, 'unknown');
  assert.equal((await transport.send(body)).status, 409);
  assert.equal(provider.calls.length, 1);
});

test('account binding (P1b): the binding is durable; after a restart with another account nothing of the previous account is served, and the bound account resumes', async (t) => {
  const spool = tmpSpool(t);
  const clock = { t: 1_700_000_000_000 };
  const a = makeTransport(spool, { clock }).transport;
  const m1 = a.ingest(BOT_ORIGIN, baileysMsg());
  assert.equal((await a.send({ chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT })).status, 200);
  const [leaseA] = a.claim({}).body.leases;
  const accountDir = path.join(spool, 'account');
  assert.ok(existsSync(accountDir), 'the spool is bound to its account on disk');
  const [bindingFile] = readdirSync(accountDir);
  assert.match(bindingFile, /^[0-9a-f]{64}\.json$/);
  assert.equal(statSync(path.join(accountDir, bindingFile)).mode & 0o777, 0o600);
  assert.equal(readJson(path.join(accountDir, bindingFile)).account_jid, BOT_CANONICAL);
  const before = spoolSnapshot(spool);

  const providerB = fakeProvider();
  const b = makeTransport(spool, { clock, provider: providerB, getAccountJid: () => ACCOUNT_B_RAW }).transport;
  assert.deepEqual(b.claim({}), ACCOUNT_DENIED);
  assert.deepEqual(b.ack({ envelope_id: leaseA.envelope_id, lease_token: leaseA.lease_token }), ACCOUNT_DENIED);
  assert.deepEqual(await b.send({ chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT }), ACCOUNT_DENIED);
  assert.deepEqual(await b.send({ chatId: GROUP_A, message: 'nova', attempt_id: 'attempt-0002-licencas' }), ACCOUNT_DENIED);
  assert.deepEqual(b.receipt(ATTEMPT), ACCOUNT_DENIED);
  assert.deepEqual(b.ingest(originOf(ACCOUNT_B_RAW), baileysMsg({ id: '3EB0OTHER0001' })), { status: 'rejected', reason: 'account_mismatch' });
  assert.equal(b.stats().account_mismatch, true);
  assert.equal(providerB.calls.length, 0);
  assert.deepEqual(spoolSnapshot(spool), before);

  const providerA = fakeProvider();
  const a2 = makeTransport(spool, { clock, provider: providerA }).transport;
  const [lease] = a2.claim({}).body.leases;
  assert.equal(lease.envelope_id, m1.envelopeId);
  assert.equal(a2.ack({ envelope_id: lease.envelope_id, lease_token: lease.lease_token }).status, 200);
  const replay = await a2.send({ chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT });
  assert.deepEqual([replay.status, replay.body.replayed, replay.body.messageIds], [200, true, ['FAKEOUT1']]);
  assert.equal(a2.receipt(ATTEMPT).body.state, 'confirmed');
  assert.equal(providerA.calls.length, 0);
});

test('account binding (P1b): an unbound legacy spool binds only to the account its records prove; divergent accounts or sends without evidence fail startup closed, deleting nothing', async (t) => {
  const unbind = (spool) => { OPEN_OWNERS.get(spool)?.close(); rmSync(path.join(spool, 'account'), { recursive: true, force: true }); };

  // Legacy (pre-binding) spool of account A: account B is refused and binds nothing; A binds and resumes.
  const legacy = tmpSpool(t);
  const a = makeTransport(legacy).transport;
  const m1 = a.ingest(BOT_ORIGIN, baileysMsg());
  assert.equal((await a.send({ chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT })).status, 200);
  unbind(legacy);
  const before = spoolSnapshot(legacy);
  const providerB = fakeProvider();
  const b = makeTransport(legacy, { provider: providerB, getAccountJid: () => ACCOUNT_B_RAW }).transport;
  assert.deepEqual(b.claim({}), ACCOUNT_DENIED);
  assert.deepEqual(await b.send({ chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT }), ACCOUNT_DENIED);
  assert.deepEqual(b.receipt(ATTEMPT), ACCOUNT_DENIED);
  assert.deepEqual(b.ingest(originOf(ACCOUNT_B_RAW), baileysMsg({ id: '3EB0OTHER0001' })), { status: 'rejected', reason: 'account_mismatch' });
  assert.equal(providerB.calls.length, 0);
  assert.deepEqual(readdirSync(path.join(legacy, 'account')), [], 'a mismatching account never binds the spool');
  assert.deepEqual(spoolSnapshot(legacy), before);
  const a2 = makeTransport(legacy).transport;
  assert.deepEqual(a2.claim({}).body.leases.map((l) => l.envelope_id), [m1.envelopeId]);
  assert.equal(readdirSync(path.join(legacy, 'account')).length, 1, 'the proven account binds before items are served');
  assert.equal(a2.receipt(ATTEMPT).body.state, 'confirmed');

  // Legacy spool whose inbox proves two different accounts: fail closed for every account.
  const divergent = tmpSpool(t);
  makeTransport(divergent).transport.ingest(BOT_ORIGIN, baileysMsg());
  unbind(divergent);
  const [file] = inboxFiles(divergent);
  const forged = { ...readJson(path.join(divergent, 'inbox', file)), envelope_id: 'f'.repeat(64), instance_id: ACCOUNT_B };
  writeFileSync(path.join(divergent, 'inbox', `${'f'.repeat(64)}.json`), JSON.stringify(forged), { mode: 0o600 });
  const divergentBefore = spoolSnapshot(divergent);
  for (const raw of [BOT_RAW, ACCOUNT_B_RAW]) {
    assert.throws(() => makeTransport(divergent, { getAccountJid: () => raw }),
      (err) => err.name === 'DurableStateError' && /more than one account/.test(err.message), raw);
  }
  assert.deepEqual(spoolSnapshot(divergent), divergentBefore);

  // Legacy spool with send records but no inbound record: no evidence of the account, fail closed.
  const sendsOnly = tmpSpool(t);
  assert.equal((await makeTransport(sendsOnly).transport.send({ chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT })).status, 200);
  unbind(sendsOnly);
  const sendsBefore = spoolSnapshot(sendsOnly);
  assert.throws(() => makeTransport(sendsOnly),
    (err) => err.name === 'DurableStateError' && /without account evidence/.test(err.message));
  assert.deepEqual(spoolSnapshot(sendsOnly), sendsBefore);

  // Bound spool holding an inbound record of another account (tampered): fail closed.
  const bound = tmpSpool(t);
  makeTransport(bound).transport.ingest(BOT_ORIGIN, baileysMsg());
  OPEN_OWNERS.get(bound)?.close();
  const [boundFile] = inboxFiles(bound);
  const other = { ...readJson(path.join(bound, 'inbox', boundFile)), envelope_id: 'e'.repeat(64), instance_id: ACCOUNT_B };
  writeFileSync(path.join(bound, 'inbox', `${'e'.repeat(64)}.json`), JSON.stringify(other), { mode: 0o600 });
  const boundBefore = spoolSnapshot(bound);
  assert.throws(() => makeTransport(bound), (err) => err.name === 'DurableStateError' && /another account/.test(err.message));
  assert.deepEqual(spoolSnapshot(bound), boundBefore);
});

// ------------------------------------------------- durability follow-up (canonical-a1-system-ca P1s)

// UNIT fault: the fsync seam throws EIO for `fault.path` (or `fault.match(path)`) while `fault.on`; every other
// call is a real fsync.
function faultyFsync(fault, synced) {
  return (fd, p) => {
    synced.push(p);
    if (fault.on && (fault.match ? fault.match(p) : p === fault.path)) throw Object.assign(new Error('EIO injected'), { code: 'EIO' });
    fsyncSync(fd);
  };
}
const BIND_FAILED = { status: 503, body: { error: 'account_bind_failed' } };
// An inbound whose account binding failed is kept in the bounded retry buffer (not durable, not claimable).
const bufferedBindFailure = (r) => ({ status: r.status, code: r.code, retained: r.retained });
const BUFFERED_BIND_FAILED = { status: 'persist_failed', code: 'account_bind_failed', retained: true };

test('account binding durability (P1): a binding renamed into account/ whose dir fsync failed stays provisional; every operation repeats the durability step and nothing is accepted until it succeeds', async (t) => {
  const spool = tmpSpool(t);
  const accountDir = path.join(spool, 'account');
  const fault = { on: false, path: accountDir };
  const synced = [];
  const timers = fakeTimers();
  const { transport, provider, logs } = makeTransport(spool, { fsync: faultyFsync(fault, synced), timers });
  fault.on = true; // the account/ directory fsync fails after startup

  assert.deepEqual(bufferedBindFailure(transport.ingest(BOT_ORIGIN, baileysMsg())), BUFFERED_BIND_FAILED);
  assert.equal(readdirSync(accountDir).length, 1, 'the binding was renamed into account/ (on disk, not known durable)');
  const mark = synced.length;
  // Still failing: the provisional binding must not count as bound for any operation.
  assert.deepEqual(bufferedBindFailure(transport.ingest(BOT_ORIGIN, baileysMsg({ id: '3EB0PROV0002' }))), BUFFERED_BIND_FAILED);
  assert.deepEqual(transport.claim({}), BIND_FAILED);
  assert.deepEqual(transport.ack({ envelope_id: 'a'.repeat(64), lease_token: 'b'.repeat(64) }), BIND_FAILED);
  assert.deepEqual(await transport.send({ chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT }), BIND_FAILED);
  assert.deepEqual(transport.receipt(ATTEMPT), BIND_FAILED);
  assert.equal(synced.slice(mark).filter((p) => p === accountDir).length, 5, 'each operation repeated the account/ fsync');
  assert.equal(provider.calls.length, 0, 'no provider call while the binding is not durable');
  assert.deepEqual(inboxFiles(spool), [], 'no inbound accepted');
  assert.deepEqual(sendFiles(spool), [], 'no send intent accepted');
  assert.equal(transport.stats().account_bound, false);
  timers.handles[0].fn(); // maintenance: the provisional binding on disk is the owner's own state, not tampering
  assert.equal(transport.stats().quarantined, false);

  fault.on = false; // the disk recovers: the next operation makes the binding durable before its own effect
  const healed = synced.length;
  const m = transport.ingest(BOT_ORIGIN, baileysMsg());
  assert.equal(m.status, 'persisted');
  const after = synced.slice(healed);
  assert.ok(after.includes(accountDir) && after.indexOf(accountDir) < after.indexOf(path.join(spool, 'inbox')),
    `account/ fsynced before the first inbound is accepted: ${JSON.stringify(after)}`);
  assert.equal(transport.stats().account_bound, true);
  const [bindingFile] = readdirSync(accountDir);
  assert.equal(readJson(path.join(accountDir, bindingFile)).account_jid, BOT_CANONICAL);
  assert.equal(logs.filter((e) => e.event === 'durable_account_bind_failed').length, 8, '6 operations + 2 buffered retries');
  assert.equal(logs.filter((e) => e.event === 'durable_account_bound').length, 1);
  assert.deepEqual(transport.claim({}).body.leases.map((l) => l.envelope_id), [m.envelopeId]);
  assert.equal((await transport.send({ chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT })).status, 200);
  assert.equal(provider.calls.length, 1);
});

test('account binding durability (P1): while the binding is provisional maintenance purges nothing, so losing the never-fsynced binding (power-loss stand-in) still leaves a loadable spool with its receipts', async (t) => {
  const spool = tmpSpool(t);
  const clock = { t: 1_700_000_000_000 };
  const a = makeTransport(spool, { clock }).transport;
  const m1 = a.ingest(BOT_ORIGIN, baileysMsg());
  const [lease] = a.claim({}).body.leases;
  assert.equal(a.ack({ envelope_id: lease.envelope_id, lease_token: lease.lease_token }).status, 200);
  clock.t += 40 * DAY_MS; // the acked inbound (the only account evidence) is past retention
  assert.equal((await a.send({ chatId: GROUP_A, message: 'ok', attempt_id: ATTEMPT })).status, 200); // recent: kept
  OPEN_OWNERS.get(spool).close();
  rmSync(path.join(spool, 'account'), { recursive: true, force: true }); // pre-binding (legacy) spool

  const accountDir = path.join(spool, 'account');
  const fault = { on: false, path: accountDir };
  const timers = fakeTimers();
  const b = makeTransport(spool, { clock, timers, fsync: faultyFsync(fault, []) }).transport;
  fault.on = true;
  assert.deepEqual(b.receipt(ATTEMPT), BIND_FAILED);
  assert.equal(readdirSync(accountDir).length, 1, 'provisional binding on disk');
  const before = spoolSnapshot(spool);
  timers.handles[0].fn();
  assert.deepEqual(spoolSnapshot(spool), before, 'no retention purge while the binding is not durable');
  assert.ok(existsSync(path.join(spool, 'inbox', `${m1.envelopeId}.json`)), 'the account evidence is kept');

  OPEN_OWNERS.get(spool).close();
  rmSync(path.join(accountDir, readdirSync(accountDir)[0])); // the un-fsynced entry is what a power loss may drop
  const c = makeTransport(spool, { clock }).transport; // must load (sends are still backed by inbound evidence)
  const receipt = c.receipt(ATTEMPT);
  assert.deepEqual([receipt.status, receipt.body.state], [200, 'confirmed']);
  assert.equal(readdirSync(accountDir).length, 1, 'bound durably once the disk works');
});

test('startup with ancestors shared with the session (P1): every directory of the real spool path is fsynced up to / before anything is accepted; a failing shared-ancestor fsync aborts startup', (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-shared-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const home = path.join(root, 'home');
  mkdirSync(home);
  // bridge.js order on a fresh install: mkdirSync(SESSION_DIR, { recursive: true }) runs before durable startup,
  // so ~/.hermes and ~/.hermes/whatsapp already exist (never fsynced) when the spool is provisioned.
  mkdirSync(path.join(home, '.hermes', 'whatsapp', 'session'), { recursive: true });
  const spool = parseDurableConfig({ WHATSAPP_DURABLE_GROUP_IDS: JSON.stringify([GROUP_A]) }, { homeDir: home }).spoolDir;
  assert.equal(spool, path.join(home, '.hermes', 'whatsapp', 'durable'));
  const shared = path.join(home, '.hermes');

  const fault = { on: true, path: shared };
  assert.throws(() => makeTransport(spool, { fsync: faultyFsync(fault, []) }), /EIO injected/);
  assert.equal(existsSync(path.join(spool, 'owner.lock')), false, 'startup aborted before ownership, load or traffic');

  // The spool dir now pre-exists too: every later start syncs the whole real chain again.
  const synced = [];
  const { transport } = makeTransport(spool, { fsync: faultyFsync({ on: false }, synced) });
  const chain = [];
  for (let dir = path.dirname(realpathSync(spool)); ; dir = path.dirname(dir)) {
    chain.push(dir);
    if (path.dirname(dir) === dir) break;
  }
  assert.ok(chain.includes(realpathSync(shared)) && chain.at(-1) === '/');
  for (const dir of chain) assert.ok(synced.includes(dir), `ancestor ${dir} fsynced at startup: ${JSON.stringify(synced)}`);
  const lastAncestorSync = Math.max(...chain.map((dir) => synced.lastIndexOf(dir)));
  assert.ok(lastAncestorSync < synced.indexOf(spool), 'the chain is durable before the spool is used');
  assert.equal(transport.ingest(BOT_ORIGIN, baileysMsg()).status, 'persisted');
});

// ------------------------------------------- binding failure on ingest (canonical-a1-durability-p1-final P1)

const retryLog = (logs) => logs.filter((e) => e.event === 'durable_inbound_retry').at(-1);
const retryState = (logs) => { const e = retryLog(logs); return [e.status, e.code, e.retained, e.attempts]; };
const ingestHealth = (s) => ({
  degraded: s.degraded, buffered: s.ingest_retry_buffered, failures: s.ingest_failures,
  recovered: s.ingest_recovered, dropped: s.ingest_dropped, bound: s.account_bound,
});

test('ingest under a failing account binding (P1): a transient failure buffers the first message (existing bounded retry), the tick retries it under the same verified account, and it is claimable only after binding and record are fsynced', async (t) => {
  const spool = tmpSpool(t);
  const accountDir = path.join(spool, 'account');
  const fault = { on: false, path: accountDir };
  const synced = [];
  const timers = fakeTimers();
  const { transport, logs } = makeTransport(spool, { fsync: faultyFsync(fault, synced), timers });
  fault.on = true;

  const r = transport.ingest(BOT_ORIGIN, baileysMsg());
  assert.deepEqual([r.status, r.code, r.retained, r.attempts], ['persist_failed', 'account_bind_failed', true, 1]);
  assert.match(r.envelopeId, HEX64);
  assert.deepEqual(inboxFiles(spool), [], 'not durable');
  assert.deepEqual(transport.claim({}), BIND_FAILED, 'never claimable before the binding is durable');
  assert.deepEqual(ingestHealth(transport.stats()),
    { degraded: true, buffered: 1, failures: 1, recovered: 0, dropped: 0, bound: false }, '/health shows the buffered message');

  timers.handles[0].fn(); // still failing: retained, attempt counted
  assert.deepEqual(retryState(logs), ['persist_failed', 'account_bind_failed', true, 2]);
  assert.deepEqual(inboxFiles(spool), []);

  fault.on = false; // transient: the disk recovers
  const healed = synced.length;
  timers.handles[0].fn();
  assert.deepEqual([retryLog(logs).status, retryLog(logs).envelope_id], ['persisted', r.envelopeId]);
  const after = synced.slice(healed);
  const inboxDir = path.join(spool, 'inbox');
  assert.ok(after.includes(accountDir) && after.indexOf(accountDir) < after.indexOf(inboxDir),
    `binding fsynced before the record: ${JSON.stringify(after)}`);
  assert.deepEqual(ingestHealth(transport.stats()),
    { degraded: false, buffered: 0, failures: 2, recovered: 1, dropped: 0, bound: true });
  const [lease] = transport.claim({}).body.leases;
  assert.equal(lease.envelope_id, r.envelopeId);
  assert.equal(inboxRecord(spool, r.envelopeId).instance_id, BOT_CANONICAL, 'persisted under the verified account');
  assert.equal(transport.ack({ envelope_id: lease.envelope_id, lease_token: lease.lease_token }).status, 200);
});

test('ingest under a failing account binding (P1): a persistent failure exhausts the existing retry bound and is dropped loudly (/health degraded + ingest_dropped), never persisted', async (t) => {
  const spool = tmpSpool(t);
  const fault = { on: false, path: path.join(spool, 'account') };
  const timers = fakeTimers();
  const { transport, logs } = makeTransport(spool, { fsync: faultyFsync(fault, []), timers });
  fault.on = true;
  assert.equal(transport.ingest(BOT_ORIGIN, baileysMsg()).retained, true);
  for (let i = 0; i < 4; i += 1) timers.handles[0].fn();
  assert.deepEqual(retryState(logs), ['persist_failed', 'account_bind_failed', false, 5]);
  assert.deepEqual(ingestHealth(transport.stats()),
    { degraded: true, buffered: 0, failures: 5, recovered: 0, dropped: 1, bound: false });
  assert.deepEqual(inboxFiles(spool), []);
  fault.on = false;
  timers.handles[0].fn();
  assert.deepEqual(transport.claim({}).body.leases, [], 'a dropped message is never served later');
  assert.equal(transport.stats().degraded, true, 'the drop keeps /health degraded until restart');
  const dump = JSON.stringify([logs, transport.stats()]);
  for (const secret of [GROUP_A, MEMBER_LID, BOT_CANONICAL, '3EB0A1B2C3D4E5F60001', 'Licença', spool]) {
    assert.equal(dump.includes(secret), false, `diagnostics leak ${secret}`);
  }
});

test('ingest under a failing account binding (P1): an account switch during the retry never persists, exposes or binds the buffered message under another (or unknown) account; it is dropped loudly', async (t) => {
  const spool = tmpSpool(t);
  const accountDir = path.join(spool, 'account');
  const idA = createHash('sha256').update(JSON.stringify(['account.v1', BOT_CANONICAL])).digest('hex');
  const bindingTmp = path.join(spool, 'tmp', `${idA}.json.`);
  const fault = { on: false, match: (p) => p.startsWith(bindingTmp) }; // binding write fails before its rename
  const account = { raw: BOT_RAW };
  const timers = fakeTimers();
  const { transport, logs } = makeTransport(spool, { fsync: faultyFsync(fault, []), timers, getAccountJid: () => account.raw });
  fault.on = true;
  const r = transport.ingest(BOT_ORIGIN, baileysMsg());
  assert.deepEqual([r.status, r.code, r.retained], ['persist_failed', 'account_bind_failed', true]);
  assert.deepEqual(readdirSync(accountDir), [], 'nothing bound yet');
  fault.on = false; // the disk is healthy from now on

  account.raw = ACCOUNT_B_RAW; // re-paired to another account while A's message waits
  timers.handles[0].fn();
  assert.deepEqual(retryState(logs), ['persist_failed', 'account_changed', true, 2]);
  assert.deepEqual(readdirSync(accountDir), [], 'a retry never binds the spool to another account');
  assert.deepEqual(inboxFiles(spool), [], 'never persisted for another account');
  assert.deepEqual(transport.claim({}).body.leases, [], 'B binds through its own claim and sees nothing of A');
  assert.equal(readJson(path.join(accountDir, readdirSync(accountDir)[0])).account_jid, ACCOUNT_B);

  account.raw = undefined; // Baileys reports no account
  timers.handles[0].fn();
  assert.deepEqual(retryState(logs), ['persist_failed', 'account_unknown', true, 3]);

  account.raw = BOT_RAW; // A is back, but the spool now belongs to B
  timers.handles[0].fn();
  assert.deepEqual(retryState(logs), ['persist_failed', 'account_mismatch', true, 4]);
  timers.handles[0].fn();
  assert.deepEqual(retryState(logs), ['persist_failed', 'account_mismatch', false, 5]);
  assert.deepEqual(inboxFiles(spool), [], 'the envelope was never written');
  assert.deepEqual(ingestHealth(transport.stats()),
    { degraded: true, buffered: 0, failures: 5, recovered: 0, dropped: 1, bound: true });
  account.raw = ACCOUNT_B_RAW;
  assert.deepEqual(transport.claim({}).body.leases, []);
  assert.equal(transport.stats().account_mismatch, false);
});

// ---------------------------- emitting socket identity (canonical-a1-binding-ingest-final P1)
//
// bridge.js's messages.upsert loop is `for (const msg of routeUpsertBatch(durable, emitter, messages, report))`
// followed by the legacy body, which awaits downloadMediaMessage for media. The router, the origin capture
// and ingest are the exported production code; upsertBatch() below only reproduces that loop shape. The
// "sockets" are Baileys-shaped `{ user: { id } }` objects (Baileys' sock.user is a getter on that socket's
// own creds), and bridge.active plays bridge.js's mutable global `sock`, read by getAccountJid exactly as
// in bridge.js (`() => sock?.user?.id`) and replaced by every reconnect (startSocket()).

const socketOf = (raw) => ({ user: { id: raw } });
const BOT_OTHER_DEVICE_RAW = BOT_RAW.replace(':7@', ':9@'); // same account, another device suffix

function makeBridge(spool, { activeRaw = BOT_RAW, ...opts } = {}) {
  const bridge = { active: socketOf(activeRaw) };
  return Object.assign(bridge, makeTransport(spool, { getAccountJid: () => bridge.active?.user?.id, ...opts }));
}

async function upsertBatch(transport, emitter, messages, legacyBody = async () => {}) {
  const reports = [];
  const legacy = [];
  for (const msg of routeUpsertBatch(transport, emitter, messages, (result) => reports.push(result))) {
    legacy.push(msg.key.id);
    await legacyBody(msg);
  }
  return { reports, legacy };
}

// A legacy (non-durable) image DM: bridge.js awaits downloadMediaMessage for it.
const legacyImage = (id) => baileysMsg({ remoteJid: DM_JID, participant: undefined, id, message: { imageMessage: { mimetype: 'image/jpeg' } } });

// The media download is pending while, on another task, the bridge reconnects: startSocket() assigns a new global sock.
async function mediaAwaitWithReconnect(bridge, raw) {
  const download = new Promise((resolve) => setTimeout(resolve, 5));
  setImmediate(() => { bridge.active = socketOf(raw); });
  await download;
}

const accountFiles = (spool) => readdirSync(path.join(spool, 'account'));
const boundAccount = (spool) => readJson(path.join(spool, 'account', accountFiles(spool)[0])).account_jid;
const ACCOUNT_CHANGED = { status: 'rejected', reason: 'account_changed' };

test('eager spool (P1): durable messages behind a legacy media message are persisted before the first legacy await and survive a bridge exit during that download (same-account restart)', async (t) => {
  const spool = tmpSpool(t);
  const bridge = makeBridge(spool);
  const batch = [
    legacyImage('3EB0LEGACY0001'), baileysMsg({ id: '3EB0EAGER0001' }), legacyImage('3EB0LEGACY0002'), baileysMsg({ id: '3EB0EAGER0002' }),
  ];
  const reports = [];
  let onDiskAtFirstAwait = null;
  for (const msg of routeUpsertBatch(bridge.transport, bridge.active, batch, (result) => reports.push(result))) {
    assert.equal(msg.key.id, '3EB0LEGACY0001');
    onDiskAtFirstAwait = inboxFiles(spool); // the legacy body is about to await downloadMediaMessage
    bridge.transport.close(); // the bridge exits during that download: nothing after this point runs
    break;
  }
  assert.deepEqual(reports.map((r) => r.status), ['persisted', 'persisted']);
  assert.deepEqual(onDiskAtFirstAwait, reports.map((r) => `${r.envelopeId}.json`).sort(), 'spooled before the first legacy await');
  const restarted = makeTransport(spool, { getAccountJid: () => BOT_OTHER_DEVICE_RAW }).transport; // same account, new process
  assert.deepEqual(restarted.claim({}).body.leases.map((l) => l.envelope.message_id).sort(), ['3EB0EAGER0001', '3EB0EAGER0002']);
});

test('emitter origin (P1): a batch emitted by A is spooled under A before its legacy media await; a reconnect to B during that await gives B no access (the empty spool binds to A, never to B); A resumes', async (t) => {
  const spool = tmpSpool(t);
  const bridge = makeBridge(spool);
  const emitterA = bridge.active;
  const batch = [legacyImage('3EB0LEGACY0001'), baileysMsg({ id: '3EB0RACE0001' }), baileysMsg({ id: '3EB0RACE0002' })];
  const { reports, legacy } = await upsertBatch(bridge.transport, emitterA, batch, () => mediaAwaitWithReconnect(bridge, ACCOUNT_B_RAW));
  assert.equal(bridge.active.user.id, ACCOUNT_B_RAW, 'the global socket was replaced during the media await');
  assert.deepEqual(legacy, ['3EB0LEGACY0001']);
  assert.deepEqual(reports.map((r) => r.status), ['persisted', 'persisted']);
  for (const r of reports) assert.equal(inboxRecord(spool, r.envelopeId).instance_id, BOT_CANONICAL);
  assert.equal(boundAccount(spool), BOT_CANONICAL, 'bound to the emitter A, never to B');
  const before = spoolSnapshot(spool);
  assert.deepEqual(bridge.transport.claim({}), ACCOUNT_DENIED, 'B has no access to A\'s messages');
  assert.deepEqual(spoolSnapshot(spool), before);
  bridge.active = socketOf(BOT_OTHER_DEVICE_RAW); // A back on a new socket
  assert.deepEqual(bridge.transport.claim({}).body.leases.map((l) => l.envelope_id).sort(), reports.map((r) => r.envelopeId).sort());
});

test('emitter origin (P1): a late batch of the old socket A, emitted after the bridge already reconnected as B, is refused: it never binds an empty spool to B nor reaches B\'s claims', async (t) => {
  const spool = tmpSpool(t);
  const bridge = makeBridge(spool);
  const emitterA = bridge.active;
  bridge.active = socketOf(ACCOUNT_B_RAW); // startSocket() already replaced the global; A's last events still flush
  const batch = [legacyImage('3EB0LEGACY0001'), baileysMsg({ id: '3EB0LATE0001' }), baileysMsg({ id: '3EB0LATE0002' })];
  const { reports, legacy } = await upsertBatch(bridge.transport, emitterA, batch);
  assert.deepEqual(legacy, ['3EB0LEGACY0001']);
  assert.deepEqual(reports, [ACCOUNT_CHANGED, ACCOUNT_CHANGED]);
  assert.deepEqual(accountFiles(spool), [], 'the old socket never binds the spool to the new account');
  assert.deepEqual(inboxFiles(spool), [], 'nothing of A is spooled as B');
  // B, the live account, binds through its own claim and sees nothing of A.
  assert.deepEqual(bridge.transport.claim({}).body.leases, []);
  assert.equal(boundAccount(spool), ACCOUNT_B);
});

test('emitter origin (P1): on a spool already bound to account B, an account-A batch is refused as a whole while A is live and stays refused after the reconnect to B; B never claims A\'s messages', async (t) => {
  const spool = tmpSpool(t);
  const bridge = makeBridge(spool, { activeRaw: ACCOUNT_B_RAW });
  const ownB = await upsertBatch(bridge.transport, bridge.active, [baileysMsg({ id: '3EB0OWNB0001' })]);
  assert.equal(ownB.reports[0].status, 'persisted');
  assert.equal(boundAccount(spool), ACCOUNT_B);
  bridge.active = socketOf(BOT_RAW); // re-paired to A
  const emitterA = bridge.active;
  const before = spoolSnapshot(spool);
  const batch = [baileysMsg({ id: '3EB0RACE0001' }), legacyImage('3EB0LEGACY0001'), baileysMsg({ id: '3EB0RACE0002' })];
  const { reports } = await upsertBatch(bridge.transport, emitterA, batch, () => mediaAwaitWithReconnect(bridge, ACCOUNT_B_RAW));
  const MISMATCH = { status: 'rejected', reason: 'account_mismatch' }; // A is live, the spool is B's
  assert.deepEqual(reports, [MISMATCH, MISMATCH]);
  assert.deepEqual(spoolSnapshot(spool), before, 'nothing of A is written');
  assert.deepEqual(bridge.transport.claim({}).body.leases.map((l) => l.envelope_id), [ownB.reports[0].envelopeId]);
});

test('emitter origin (P1): on a spool already bound to A, a later A batch is spooled under A before its media await; after a reconnect to B nothing of A is served to B; A resumes with every message', async (t) => {
  const spool = tmpSpool(t);
  const bridge = makeBridge(spool);
  const first = await upsertBatch(bridge.transport, bridge.active, [baileysMsg({ id: '3EB0BOUND0001' })]);
  assert.equal(boundAccount(spool), BOT_CANONICAL);
  const emitterA = bridge.active;
  const batch = [legacyImage('3EB0LEGACY0001'), baileysMsg({ id: '3EB0RACE0001' }), baileysMsg({ id: '3EB0RACE0002' })];
  const { reports } = await upsertBatch(bridge.transport, emitterA, batch, () => mediaAwaitWithReconnect(bridge, ACCOUNT_B_RAW));
  assert.deepEqual(reports.map((r) => r.status), ['persisted', 'persisted']);
  assert.deepEqual(bridge.transport.claim({}), ACCOUNT_DENIED);
  bridge.active = socketOf(BOT_OTHER_DEVICE_RAW); // A back on a new socket
  const all = [...first.reports, ...reports].map((r) => r.envelopeId).sort();
  assert.deepEqual(bridge.transport.claim({}).body.leases.map((l) => l.envelope_id).sort(), all);
});

test('emitter origin (P1): a reconnect to the SAME account during the media await keeps the batch valid; every durable message spools under that account', async (t) => {
  const spool = tmpSpool(t);
  const bridge = makeBridge(spool);
  const emitterA = bridge.active;
  const batch = [legacyImage('3EB0LEGACY0001'), baileysMsg({ id: '3EB0SAME0001' }), legacyImage('3EB0LEGACY0002'), baileysMsg({ id: '3EB0SAME0002' })];
  const { reports, legacy } = await upsertBatch(bridge.transport, emitterA, batch, () => mediaAwaitWithReconnect(bridge, BOT_OTHER_DEVICE_RAW));
  assert.notEqual(bridge.active, emitterA, 'the global socket was replaced');
  assert.deepEqual(legacy, ['3EB0LEGACY0001', '3EB0LEGACY0002']);
  assert.deepEqual(reports.map((r) => r.status), ['persisted', 'persisted']);
  for (const r of reports) assert.equal(inboxRecord(spool, r.envelopeId).instance_id, BOT_CANONICAL);
  assert.equal(boundAccount(spool), BOT_CANONICAL);
  assert.deepEqual(bridge.transport.claim({}).body.leases.map((l) => l.envelope_id).sort(), reports.map((r) => r.envelopeId).sort());
});

test('emitter origin: routeUpsertBatch yields every non-durable message in order (all of them when durable is off); a throwing ingest is reported as an error and the batch goes on', async (t) => {
  const spool = tmpSpool(t);
  const { transport } = makeTransport(spool);
  const sockA = socketOf(BOT_RAW);
  const batch = [
    legacyImage('3EB0LEGACY0001'), baileysMsg({ id: '3EB0ROUTE0001' }),
    baileysMsg({ remoteJid: GROUP_UNLISTED, id: '3EB0LEGACY0002' }), baileysMsg({ remoteJid: GROUP_B, id: '3EB0ROUTE0002' }),
  ];
  const on = await upsertBatch(transport, sockA, batch);
  assert.deepEqual(on.legacy, ['3EB0LEGACY0001', '3EB0LEGACY0002']);
  assert.deepEqual(on.reports.map((r) => r.status), ['persisted', 'persisted']);
  const off = await upsertBatch(null, sockA, batch);
  assert.deepEqual(off.legacy, batch.map((m) => m.key.id));
  assert.deepEqual(off.reports, []);
  const failing = { isDurableGroup: () => true, ingest: () => { throw Object.assign(new Error('boom'), { code: 'EIO' }); } };
  const failed = await upsertBatch(failing, sockA, batch.slice(0, 2));
  assert.deepEqual(failed, { reports: [{ status: 'error', code: 'EIO' }, { status: 'error', code: 'EIO' }], legacy: [] });
});

test('emitter origin (P1): ingest accepts only a frozen origin captured from a socket; a missing or forged origin, or one without an account, is refused without effect', (t) => {
  const spool = tmpSpool(t);
  const { transport } = makeTransport(spool);
  const before = spoolSnapshot(spool);
  for (const forged of [undefined, null, BOT_RAW, BOT_CANONICAL, { account: BOT_CANONICAL }, Object.freeze({ account: BOT_CANONICAL })]) {
    assert.deepEqual(transport.ingest(forged, baileysMsg()), { status: 'rejected', reason: 'origin_unknown' }, String(forged));
  }
  for (const socket of [null, {}, { user: undefined }, socketOf(undefined), socketOf('not-a-jid')]) {
    assert.deepEqual(transport.ingest(captureInboundOrigin(socket), baileysMsg()), { status: 'rejected', reason: 'account_unknown' });
  }
  assert.deepEqual(spoolSnapshot(spool), before);
  assert.deepEqual(accountFiles(spool), []);
  const sock = socketOf(BOT_RAW);
  const origin = captureInboundOrigin(sock);
  assert.ok(Object.isFrozen(origin));
  assert.deepEqual({ ...origin }, { account: BOT_CANONICAL });
  sock.user = { id: ACCOUNT_B_RAW }; // a snapshot: later changes of the socket do not move the origin
  assert.equal(origin.account, BOT_CANONICAL);
  assert.equal(transport.ingest(origin, baileysMsg()).status, 'persisted');
});
