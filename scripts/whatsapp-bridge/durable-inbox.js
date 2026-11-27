/**
 * Durable WhatsApp group transport for the Hermes bridge (unit A1).
 *
 * #atende:SPEC-licencas-acompanhamento/REQ-R4 (isolamento: opt-in exact group whitelist, DMs/other groups untouched)
 * #atende:SPEC-licencas-acompanhamento/REQ-R5 (concorrência: exclusive leases, in-flight attempt guard)
 * #atende:SPEC-licencas-acompanhamento/REQ-R8 (falha: fsync'd spool, replay until ack, delivery_unknown never resends)
 * #atende:SPEC-licencas-acompanhamento/REQ-R2 (resposta auditável: send ledger + read-only receipt)
 *
 * Technical persistence only (no business state, no SQLite). Contract and
 * limits: DURABLE_TRANSPORT.md. This module performs no network I/O itself;
 * the bridge injects the Baileys send function.
 */
import { spawnSync } from 'node:child_process';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import {
  chmodSync, closeSync, constants as fsConstants, existsSync, fchmodSync, fstatSync, fsyncSync, lstatSync, mkdirSync,
  openSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, writeSync,
} from 'node:fs';
import path from 'node:path';

const INBOX_SCHEMA = 'hermes.whatsapp.durable-inbound.v1';
const LEDGER_SCHEMA = 'hermes.whatsapp.send-ledger.v1';
const ACCOUNT_SCHEMA = 'hermes.whatsapp.durable-account.v1';

const GROUP_JID_RE = /^[0-9]{6,32}(?:-[0-9]{6,20})?@g\.us$/;
const JID_RE = /^[A-Za-z0-9._:+-]{1,96}@[a-z0-9.-]{1,32}$/;
const ACCOUNT_JID_RE = /^([A-Za-z0-9._+-]{1,96})(?::[0-9]{1,6})?@([a-z0-9.-]{1,32})$/;
const MESSAGE_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;
const ATTEMPT_ID_RE = /^[A-Za-z0-9._:-]{8,128}$/;
const HEX64_RE = /^[0-9a-f]{64}$/;
const RECORD_FILE_RE = /^[0-9a-f]{64}\.json$/;
// Exact name writeJsonAtomic gives its tmp file: <record name>.<16 hex>.tmp (every record name is <64 hex>.json).
const TMP_FILE_RE = /^[0-9a-f]{64}\.json\.[0-9a-f]{16}\.tmp$/;
// Legacy bridge routes that produce or alter WhatsApp messages. In the bridge their
// guards are registered on the SAME Express paths (installDurableRoutes), so Express's
// own matcher decides. For non-Express callers the regexes below are what Express 4
// (path-to-regexp 0.1.x, router defaults sensitive:false strict:false end:true)
// compiles for these paths: case-insensitive, one optional trailing slash. The test
// suite proves the equivalence against real Express on a hostile corpus.
export const LEGACY_MESSAGE_ROUTES = Object.freeze(['/send', '/edit', '/send-media']);
const LEGACY_ROUTE_RES = new Map(LEGACY_MESSAGE_ROUTES.map((r) => [r, new RegExp(`^${r.replace(/[/-]/g, (c) => `\\${c}`)}\\/?$`, 'i')]));
const DURABLE_NS_RE = /^\/durable(?:\/|$)/i;
const CLAIM_PATH_RE = /^\/durable\/inbox\/claim\/?$/i;
const ACK_PATH_RE = /^\/durable\/inbox\/ack\/?$/i;
const RECEIPT_PATH_RE = /^\/durable\/send\/receipt\/([^/]+?)\/?$/i;
// C0/C1 control characters except \t (\u0009), \n (\u000A) and \r (\u000D).
const CONTROL_CHARS_RE = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/g;

// Technical limits; intent of each value is documented in DURABLE_TRANSPORT.md.
const KNOWN_ENV = new Set([
  'WHATSAPP_DURABLE_GROUP_IDS', 'WHATSAPP_DURABLE_SPOOL_DIR', 'WHATSAPP_DURABLE_LEASE_MS',
  'WHATSAPP_DURABLE_RETENTION_DAYS', 'WHATSAPP_DURABLE_MAX_TEXT_CHARS',
]);
const MAX_GROUPS = 32;
const LEASE_MS = { def: 120_000, min: 5_000, max: 900_000 };
const RETENTION_DAYS = { def: 35, min: 31, max: 3650 };
const MAX_TEXT_CHARS = { def: 8192, min: 256, max: 65_536 };
const CLAIM_LIMIT = { def: 10, max: 50 };
const MAX_OUTBOUND_CHARS = 65_536;
const MAX_PUSH_NAME_CHARS = 128;
const MAX_MIMETYPE_CHARS = 128;
const MAX_FILE_NAME_CHARS = 255;
const MAX_ERROR_CHARS = 300;
const DAY_MS = 86_400_000;
// Ownership: kernel advisory lock (flock(2) via util-linux flock(1)) on <spool>/owner.lock.
const LOCK_FILE = 'owner.lock';
const FLOCK_CANDIDATES = ['/usr/bin/flock', '/bin/flock'];
const FLOCK_CONFLICT_EXIT = 75;
const FLOCK_TIMEOUT_MS = 5_000;
// Maintenance tick (retention purge, spool integrity check, inbound write retry). A
// plain unref'd interval inside the bridge process, not a Hermes cron job.
const MAINTENANCE_INTERVAL_MS = 60_000;
// Inbound records whose spool write failed: kept in memory (not durable, not
// claimable) for a few ticks. Bounded in count and attempts, then dropped loudly.
const INGEST_RETRY = { maxBuffered: 64, maxAttempts: 5 };

const ATTACHMENT_FIELDS = [
  ['imageMessage', 'image'], ['videoMessage', 'video'], ['documentMessage', 'document'],
  ['audioMessage', 'audio'], ['stickerMessage', 'sticker'],
];

export class DurableConfigError extends Error {
  constructor(message) { super(message); this.name = 'DurableConfigError'; }
}

export class DurableStateError extends Error {
  constructor(message) { super(message); this.name = 'DurableStateError'; }
}

export class DurableOwnershipError extends Error {
  constructor(message) { super(message); this.name = 'DurableOwnershipError'; }
}

const matches = (re, value) => typeof value === 'string' && re.test(value);
const sha256Hex = (value) => createHash('sha256').update(value).digest('hex');
const iso = (ms) => new Date(ms).toISOString();
const reply = (status, body) => ({ status, body });
const badRequest = (detail) => reply(400, { error: 'invalid_request', detail });

function sameHex(a, b) {
  return matches(HEX64_RE, a) && matches(HEX64_RE, b) && timingSafeEqual(Buffer.from(a, 'hex'), Buffer.from(b, 'hex'));
}

function parseBoundedInt(env, name, bounds) {
  const raw = env[name];
  if (raw === undefined) return bounds.def;
  if (!/^[0-9]{1,10}$/.test(raw)) throw new DurableConfigError(`${name} must be a plain integer`);
  const value = Number(raw);
  if (value < bounds.min || value > bounds.max) {
    throw new DurableConfigError(`${name} must be between ${bounds.min} and ${bounds.max}`);
  }
  return value;
}

/**
 * Parse the opt-in durable transport config. Absent WHATSAPP_DURABLE_GROUP_IDS
 * keeps legacy behaviour; anything set but invalid throws (fail closed).
 */
export function parseDurableConfig(env, { homeDir }) {
  const durableVars = Object.keys(env).filter((k) => k.startsWith('WHATSAPP_DURABLE_'));
  const unknown = durableVars.filter((k) => !KNOWN_ENV.has(k));
  if (unknown.length) throw new DurableConfigError(`unknown durable setting(s): ${unknown.join(', ')}`);

  const rawGroups = env.WHATSAPP_DURABLE_GROUP_IDS;
  if (rawGroups === undefined) {
    if (durableVars.length) {
      throw new DurableConfigError(`${durableVars.join(', ')} set without WHATSAPP_DURABLE_GROUP_IDS`);
    }
    return { enabled: false };
  }

  let groups;
  try {
    groups = JSON.parse(rawGroups);
  } catch (err) {
    throw new DurableConfigError(`WHATSAPP_DURABLE_GROUP_IDS is not valid JSON: ${err.message}`);
  }
  if (!Array.isArray(groups) || groups.length < 1 || groups.length > MAX_GROUPS) {
    throw new DurableConfigError(`WHATSAPP_DURABLE_GROUP_IDS must be a JSON array of 1..${MAX_GROUPS} group JIDs`);
  }
  groups.forEach((jid, index) => {
    if (!matches(GROUP_JID_RE, jid)) {
      throw new DurableConfigError(`WHATSAPP_DURABLE_GROUP_IDS[${index}] is not an exact group JID (<digits>@g.us)`);
    }
  });
  if (new Set(groups).size !== groups.length) {
    throw new DurableConfigError('WHATSAPP_DURABLE_GROUP_IDS contains duplicates');
  }

  let spoolDir;
  if (env.WHATSAPP_DURABLE_SPOOL_DIR !== undefined) {
    if (!path.isAbsolute(env.WHATSAPP_DURABLE_SPOOL_DIR)) {
      throw new DurableConfigError('WHATSAPP_DURABLE_SPOOL_DIR must be an absolute path');
    }
    spoolDir = path.resolve(env.WHATSAPP_DURABLE_SPOOL_DIR);
  } else {
    // Canonical Hermes base dir: HERMES_HOME, else ~/.hermes (same rule as hermes_constants.get_hermes_home).
    const base = env.HERMES_HOME ? env.HERMES_HOME : (homeDir ? path.join(homeDir, '.hermes') : '');
    if (!path.isAbsolute(base)) {
      throw new DurableConfigError('cannot resolve spool dir: set WHATSAPP_DURABLE_SPOOL_DIR or an absolute HERMES_HOME');
    }
    spoolDir = path.join(path.resolve(base), 'whatsapp', 'durable');
  }

  return {
    enabled: true,
    groupJids: Object.freeze([...groups]),
    spoolDir,
    leaseMs: parseBoundedInt(env, 'WHATSAPP_DURABLE_LEASE_MS', LEASE_MS),
    retentionMs: parseBoundedInt(env, 'WHATSAPP_DURABLE_RETENTION_DAYS', RETENTION_DAYS) * DAY_MS,
    maxTextChars: parseBoundedInt(env, 'WHATSAPP_DURABLE_MAX_TEXT_CHARS', MAX_TEXT_CHARS),
  };
}

// ---------------------------------------------------------------- Baileys content helpers (shared with bridge.js)

export function getMessageContent(msg) {
  const content = msg?.message || {};
  if (content.ephemeralMessage?.message) return content.ephemeralMessage.message;
  if (content.viewOnceMessage?.message) return content.viewOnceMessage.message;
  if (content.viewOnceMessageV2?.message) return content.viewOnceMessageV2.message;
  if (content.documentWithCaptionMessage?.message) return content.documentWithCaptionMessage.message;
  if (content.templateMessage?.hydratedTemplate) return content.templateMessage.hydratedTemplate;
  if (content.buttonsMessage) return content.buttonsMessage;
  if (content.listMessage) return content.listMessage;
  return content;
}

export function getContextInfo(messageContent) {
  if (!messageContent || typeof messageContent !== 'object') return {};
  for (const value of Object.values(messageContent)) {
    if (value && typeof value === 'object' && value.contextInfo) {
      return value.contextInfo;
    }
  }
  return {};
}

/** Bot account JID from Baileys `sock.user.id` with the device suffix removed. */
function canonicalAccountJid(raw) {
  const m = typeof raw === 'string' ? ACCOUNT_JID_RE.exec(raw) : null;
  return m ? `${m[1]}@${m[2]}` : null;
}

function sanitizeText(value) {
  return String(value).toWellFormed().replace(CONTROL_CHARS_RE, '');
}

function limitChars(value, max) {
  const codePoints = Array.from(value);
  const truncated = codePoints.length > max;
  return { text: truncated ? codePoints.slice(0, max).join('') : value, truncated, originalChars: codePoints.length };
}

function optionalShortText(value, max) {
  if (typeof value !== 'string' || value === '') return null;
  return limitChars(sanitizeText(value), max).text;
}

function toSafeInteger(value) {
  if (typeof value === 'number') return Number.isSafeInteger(value) ? value : null;
  if (typeof value === 'string' && /^[0-9]{1,15}$/.test(value)) return Number(value);
  if (value && typeof value.toNumber === 'function') {
    const n = value.toNumber();
    return Number.isSafeInteger(n) ? n : null;
  }
  return null;
}

function extractContent(content, maxTextChars) {
  let rawText = null;
  let attachment = null;
  if (typeof content.conversation === 'string' && content.conversation) {
    rawText = content.conversation;
  } else if (typeof content.extendedTextMessage?.text === 'string' && content.extendedTextMessage.text) {
    rawText = content.extendedTextMessage.text;
  } else {
    for (const [field, kind] of ATTACHMENT_FIELDS) {
      const media = content[field];
      if (!media || typeof media !== 'object') continue;
      // Metadata only: no download, and never url/mediaKey/directPath (media decryption material).
      const baseName = typeof media.fileName === 'string' ? media.fileName.split(/[\\/]/).pop() : null;
      attachment = {
        kind,
        mimetype: optionalShortText(media.mimetype, MAX_MIMETYPE_CHARS),
        file_name: kind === 'document' ? optionalShortText(baseName, MAX_FILE_NAME_CHARS) : null,
        file_length: toSafeInteger(media.fileLength),
        seconds: toSafeInteger(media.seconds),
        ptt: kind === 'audio' && media.ptt === true,
        downloaded: false,
      };
      rawText = typeof media.caption === 'string' ? media.caption : '';
      break;
    }
  }
  if (rawText === null) return null;
  const { text, truncated, originalChars } = limitChars(sanitizeText(rawText), maxTextChars);
  return {
    content_type: attachment ? attachment.kind : 'text',
    text,
    text_truncated: truncated,
    text_original_chars: originalChars,
    attachment,
  };
}

// ---------------------------------------------------------------- filesystem (atomic, fsync'd, private)

const ownUid = (uid) => typeof process.getuid !== 'function' || uid === process.getuid();

/** Spool-owned directory: real directory (lstat, no symlink), owned by the bridge user, mode 0700. */
function assertPrivateDir(dir) {
  const st = lstatSync(dir);
  if (st.isSymbolicLink() || !st.isDirectory()) throw new DurableStateError(`${dir} is not a real directory`);
  if (!ownUid(st.uid)) throw new DurableStateError(`${dir} is not owned by the bridge user`);
  chmodSync(dir, 0o700);
}

/**
 * fsync a directory so the entries created in it are durable. Spool-owned dirs are
 * opened O_NOFOLLOW; pre-existing ancestors (follow: true) may legitimately be symlinks.
 */
function syncDir(dir, fsync, { follow = false } = {}) {
  const fd = openSync(dir, fsConstants.O_RDONLY | fsConstants.O_DIRECTORY | (follow ? 0 : fsConstants.O_NOFOLLOW));
  try { fsync(fd, dir); } finally { closeSync(fd); }
}

/**
 * Durable "mkdir -p": each missing component is created one at a time with mode 0700,
 * validated without following symlinks, and its entry fsynced into its parent before
 * the next component is created. Pre-existing ancestors are never chmod'ed.
 */
function provisionDir(dir, fsync) {
  const missing = [];
  for (let cur = dir; ; cur = path.dirname(cur)) {
    try {
      lstatSync(cur);
      break;
    } catch (err) {
      if (err.code !== 'ENOENT') throw err;
      if (path.dirname(cur) === cur) throw new DurableStateError(`no existing ancestor for ${dir}`);
      missing.push(cur);
    }
  }
  for (const component of missing.reverse()) {
    try {
      mkdirSync(component, { mode: 0o700 });
    } catch (err) {
      if (err.code !== 'EEXIST') throw err; // concurrent creator: validated below like any other
    }
    assertPrivateDir(component);
    syncDir(path.dirname(component), fsync, { follow: true });
  }
  assertPrivateDir(dir);
}

/**
 * fsync every directory of the spool's real path (realpath), from its parent up to "/", so
 * the spool's own entry and every ancestor's entry are durable even when an ancestor was
 * created before startup by someone else (bridge.js `mkdirSync(SESSION_DIR, { recursive })`
 * shares ~/.hermes/whatsapp with the spool). Runs on every start; any failure aborts startup.
 */
function syncRealAncestors(dir, fsync) {
  for (let cur = path.dirname(realpathSync(dir)); ; cur = path.dirname(cur)) {
    syncDir(cur, fsync, { follow: true });
    if (path.dirname(cur) === cur) return;
  }
}

function resolveFlock(override) {
  const found = (override ? [override] : FLOCK_CANDIDATES).find((candidate) => existsSync(candidate));
  if (!found) throw new DurableOwnershipError('flock(1) not found: exclusive spool ownership cannot be guaranteed');
  return found;
}

function runFlock(flockBin, fd) {
  // fd 3 in the helper is the caller's open file description (no path, no shell).
  return spawnSync(flockBin, ['--exclusive', '--nonblock', '--conflict-exit-code', String(FLOCK_CONFLICT_EXIT), '3'], {
    stdio: ['ignore', 'ignore', 'ignore', fd], env: {}, timeout: FLOCK_TIMEOUT_MS, killSignal: 'SIGKILL',
  });
}

/**
 * Exclusive spool ownership with a Linux kernel advisory lock. flock(2) locks belong
 * to the open file description, so after the flock(1) helper exits the lock stays
 * with the fd this process keeps open, and the kernel drops it when this process dies
 * for any reason (SIGKILL included). No PID file, no staleness heuristics, no stealing:
 * while the owner lives every contender fails. Returns the fd to keep open.
 */
function acquireOwnership(spoolDir, fsync, flockBin) {
  const lockPath = path.join(spoolDir, LOCK_FILE);
  const fd = openSync(lockPath, fsConstants.O_RDWR | fsConstants.O_CREAT | fsConstants.O_NOFOLLOW, 0o600);
  try {
    const st = fstatSync(fd);
    if (!st.isFile() || st.nlink !== 1 || !ownUid(st.uid)) throw new DurableStateError(`${lockPath} is not a private regular file`);
    fchmodSync(fd, 0o600);
    syncDir(spoolDir, fsync);
    const res = runFlock(flockBin, fd);
    if (res.error) throw new DurableOwnershipError(`flock(1) did not run: ${res.error.code ?? res.error.message}`);
    if (res.status === FLOCK_CONFLICT_EXIT) {
      throw new DurableOwnershipError(`spool ${spoolDir} is owned by another live bridge process`);
    }
    if (res.status !== 0) throw new DurableOwnershipError(`flock(1) failed (${res.status ?? res.signal})`);
    // Prove the lock is effective on this filesystem: a second open file description must conflict.
    const probe = openSync(lockPath, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
    let probeRes;
    try { probeRes = runFlock(flockBin, probe); } finally { closeSync(probe); }
    if (probeRes.status !== FLOCK_CONFLICT_EXIT) {
      throw new DurableOwnershipError('kernel advisory lock is not effective on the spool filesystem');
    }
    return fd;
  } catch (err) {
    closeSync(fd);
    throw err;
  }
}

function writeJsonAtomic(dir, tmpDir, name, value, fsync) {
  const tmpPath = path.join(tmpDir, `${name}.${randomBytes(8).toString('hex')}.tmp`);
  const data = Buffer.from(JSON.stringify(value), 'utf8');
  let renamed = false;
  try {
    const fd = openSync(tmpPath, 'wx', 0o600);
    try {
      fchmodSync(fd, 0o600);
      let offset = 0;
      while (offset < data.length) offset += writeSync(fd, data, offset);
      fsync(fd, tmpPath);
    } finally {
      closeSync(fd);
    }
    renameSync(tmpPath, path.join(dir, name));
    renamed = true;
    syncDir(dir, fsync);
  } catch (err) {
    if (renamed) err.renamed = true; // the final name may exist on disk but is not known durable
    if (!renamed) {
      try {
        rmSync(tmpPath, { force: true });
      } catch (cleanupErr) {
        err.message += ` (tmp cleanup failed: ${cleanupErr.message})`;
      }
    }
    throw err;
  }
}

function loadRecords(dir, schema, idField) {
  const records = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (!RECORD_FILE_RE.test(name)) throw new DurableStateError(`unexpected entry in spool: ${full}`);
    if (!lstatSync(full).isFile()) throw new DurableStateError(`spool entry is not a regular file: ${full}`);
    let record;
    try {
      record = JSON.parse(readFileSync(full, 'utf8'));
    } catch (err) {
      throw new DurableStateError(`corrupt record ${full}: ${err.message}`);
    }
    if (record?.schema !== schema || record[idField] !== name.slice(0, -'.json'.length)) {
      throw new DurableStateError(`corrupt record ${full}: schema/id mismatch`);
    }
    records.push(record);
  }
  return records;
}

/**
 * Crash leftovers of writeJsonAtomic. EVERY entry is validated first (exact generated
 * name, private regular file of the bridge user, single link); one foreign entry fails
 * startup closed and nothing at all is deleted.
 */
function discardTmpFiles(tmpDir) {
  const names = readdirSync(tmpDir);
  for (const name of names) {
    const full = path.join(tmpDir, name);
    const st = lstatSync(full);
    if (!TMP_FILE_RE.test(name) || !st.isFile() || st.nlink !== 1 || !ownUid(st.uid) || (st.mode & 0o077) !== 0) {
      throw new DurableStateError(`unexpected entry in spool tmp: ${full}`);
    }
  }
  for (const name of names) rmSync(path.join(tmpDir, name));
  return names.length;
}

function checkBodyKeys(body, allowed) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return badRequest('JSON object body required');
  const extra = Object.keys(body).filter((k) => !allowed.includes(k));
  return extra.length ? badRequest(`unknown field(s): ${extra.join(', ')}`) : null;
}

/** Sanitized error code for diagnostics: never paths, message text or JIDs. */
function errorCode(err) {
  if (typeof err?.code === 'string' && /^[A-Z0-9_]{1,32}$/.test(err.code)) return err.code;
  return typeof err?.name === 'string' && /^[A-Za-z]{1,40}$/.test(err.name) ? err.name : 'Error';
}

function errorText(err) {
  return limitChars(sanitizeText(err?.message ?? String(err)), MAX_ERROR_CHARS).text;
}

// ---------------------------------------------------------------- transport

/**
 * deps (all I/O injected so the module never touches WhatsApp directly):
 *   sendMessage(jid, payload, options) -> Promise<WAMessage>  Baileys send (with timeout)
 *   isConnected() -> boolean
 *   prepareChunks(message) -> string[]                       bridge formatting + splitting
 *   getAccountJid() -> string|undefined                       Baileys sock.user.id of the LIVE socket
 *   betweenChunks() -> Promise<void>                          bridge chunk pacing
 *   onSent?(WAMessage), now?() -> ms, log?(object)
 *   fsync?(fd, path)        fsync seam (UNIT fault injection); default fs.fsyncSync
 *   timers?, maintenanceIntervalMs?, flockPath?   technical seams for tests
 *
 * Startup order: provision spool root (fsync'd) -> exclusive kernel lock -> provision
 * subdirs -> discard tmp -> load -> check account binding -> purge -> start maintenance.
 * Any failure throws before anything is cleaned, loaded or sent. close() stops the timer
 * and releases the lock.
 *
 * Account boundary: the spool belongs to ONE WhatsApp account, the one Baileys reports
 * (getAccountJid). The first known account binds it durably (account/<sha256>.json) before
 * any item is served; afterwards an unknown or different account is refused by ingest,
 * claim, ack, send and receipt without exposure or effect. ingest(origin, msg) also requires the
 * emitting socket's origin (captureInboundOrigin / routeUpsertBatch) to be the live account.
 */
export function createDurableTransport(config, deps) {
  if (!config?.enabled) throw new TypeError('createDurableTransport requires an enabled durable config');
  for (const fn of ['sendMessage', 'isConnected', 'prepareChunks', 'getAccountJid', 'betweenChunks']) {
    if (typeof deps?.[fn] !== 'function') throw new TypeError(`deps.${fn} must be a function`);
  }
  const now = deps.now ?? Date.now;
  const log = deps.log ?? ((entry) => console.log(JSON.stringify(entry)));
  const fsync = deps.fsync ?? ((fd) => fsyncSync(fd));
  const timers = deps.timers ?? { setInterval, clearInterval };
  const maintenanceIntervalMs = deps.maintenanceIntervalMs ?? MAINTENANCE_INTERVAL_MS;
  const flockBin = resolveFlock(deps.flockPath);
  const groups = new Set(config.groupJids);
  const dirs = {
    inbox: path.join(config.spoolDir, 'inbox'),
    sends: path.join(config.spoolDir, 'sends'),
    tmp: path.join(config.spoolDir, 'tmp'),
    account: path.join(config.spoolDir, 'account'),
  };
  const inbox = new Map(); // envelope_id -> { record, lease: { tokenSha, expiresAt } | null, deliveryCount }
  const ledger = new Map(); // attempt_key -> record
  const inFlight = new Set();
  const retryBuffer = new Map(); // envelope_id -> { record, attempts, code }: NOT durable, never claimable
  const unsyncedInbox = new Set(); // renamed into inbox/ but the dir fsync failed: may exist on disk
  const health = { ingestFailures: 0, ingestRecovered: 0, ingestDropped: 0, lastIngestFailure: null };
  let quarantine = null;
  // { id, account, record, durable }: the account this spool belongs to. durable=false: renamed into
  // account/ (so it mirrors disk) but its dir fsync failed; nothing is accepted until a retry succeeds.
  let binding = null;
  let legacyInboxAccounts = null; // unbound pre-binding spool: the one account its inbox proves
  let closed = false;
  let lockFd = null;
  let maintenanceTimer = null;
  const unavailableReply = () => {
    if (closed) return reply(503, { error: 'durable_closed' });
    return quarantine ? reply(503, { error: 'durable_quarantined' }) : null;
  };

  const inboxKey = (instanceId, groupJid, messageId) => sha256Hex(JSON.stringify(['inbound.v1', instanceId, groupJid, messageId]));
  const attemptKey = (attemptId) => sha256Hex(JSON.stringify(['send.v1', attemptId]));
  const accountKey = (account) => sha256Hex(JSON.stringify(['account.v1', account]));
  const isDurableGroup = (jid) => typeof jid === 'string' && groups.has(jid);
  const liveAccount = () => canonicalAccountJid(deps.getAccountJid());
  const accountMismatch = (account) => (binding
    ? binding.account !== account
    : legacyInboxAccounts !== null && !legacyInboxAccounts.has(account));
  const accountReply = (error) => reply(error === 'account_mismatch' ? 409 : 503, { error });

  /**
   * The account boundary for every operation that serves or changes spool state. Binds an
   * unbound spool to the live account (durable record, written before anything is served).
   * Returns { account } or { error }; an error has had no effect.
   */
  function accountGate() {
    const account = liveAccount();
    if (!account) return { error: 'account_unknown' };
    if (accountMismatch(account)) return { error: 'account_mismatch' };
    if (binding?.durable) return { account };
    if (closed || quarantine) return { error: 'account_unbound' };
    // Unbound, or provisional (same account, by accountMismatch): the full atomic write is repeated,
    // so a successful dir fsync always covers a fresh rename of the same record.
    const id = accountKey(account);
    const t = now();
    const record = binding?.record ?? {
      schema: ACCOUNT_SCHEMA, binding_id: id, account_jid: account, bound_at: iso(t), bound_at_ms: t,
    };
    try {
      writeJsonAtomic(dirs.account, dirs.tmp, `${id}.json`, record, fsync);
    } catch (err) {
      // On disk but not known durable: mirror disk, stay provisional.
      if (err.renamed || binding) binding = { id, account, record, durable: false };
      log({ event: 'durable_account_bind_failed', code: errorCode(err) });
      return { error: 'account_bind_failed' };
    }
    binding = { id, account, record, durable: true };
    legacyInboxAccounts = null;
    log({ event: 'durable_account_bound' });
    return { account };
  }

  function purgeExpired() {
    // An unbound or provisionally bound spool is not modified (legacy account evidence is kept).
    if (!binding?.durable) return;
    const cutoff = now() - config.retentionMs;
    let inboxRemoved = 0;
    let sendsRemoved = 0;
    for (const [key, entry] of inbox) {
      if (entry.record.state === 'acked' && entry.record.acked_at_ms < cutoff) {
        rmSync(path.join(dirs.inbox, `${key}.json`));
        inbox.delete(key);
        inboxRemoved += 1;
      }
    }
    // Only confirmed attempts expire: forgetting an uncertain attempt would allow a resend.
    for (const [key, record] of ledger) {
      if (record.state === 'confirmed' && record.updated_at_ms < cutoff && !inFlight.has(key)) {
        rmSync(path.join(dirs.sends, `${key}.json`));
        ledger.delete(key);
        sendsRemoved += 1;
      }
    }
    if (inboxRemoved) syncDir(dirs.inbox, fsync);
    if (sendsRemoved) syncDir(dirs.sends, fsync);
    if (inboxRemoved || sendsRemoved) log({ event: 'durable_retention_purged', inbox: inboxRemoved, sends: sendsRemoved });
  }

  /** On-disk records must mirror the owner's state; anything else is tampering (quarantine). */
  function verifySpool() {
    const check = (dir, known, mayExist) => {
      const seen = new Set();
      for (const name of readdirSync(dir)) {
        if (!RECORD_FILE_RE.test(name) || !lstatSync(path.join(dir, name)).isFile()) {
          throw new DurableStateError(`unexpected entry in spool ${path.basename(dir)}`);
        }
        const id = name.slice(0, -'.json'.length);
        if (!known.has(id) && !mayExist(id)) throw new DurableStateError(`unknown record in spool ${path.basename(dir)}`);
        seen.add(id);
      }
      for (const id of known.keys()) {
        if (!seen.has(id)) throw new DurableStateError(`record vanished from spool ${path.basename(dir)}`);
      }
    };
    check(dirs.inbox, inbox, (id) => retryBuffer.has(id) || unsyncedInbox.has(id));
    check(dirs.sends, ledger, () => false);
    check(dirs.account, new Map(binding ? [[binding.id, binding]] : []), () => false);
  }

  /** Maintenance tick: never depends on a consumer claiming or on a restart. */
  function maintenance() {
    if (closed || quarantine) return;
    for (const entry of [...retryBuffer.values()]) {
      const result = persistInbound(entry);
      log({
        event: 'durable_inbound_retry', status: result.status, envelope_id: result.envelopeId, attempts: entry.attempts,
        ...(result.status === 'persisted' ? {} : { code: result.code, retained: result.retained }),
      });
    }
    try {
      verifySpool();
      purgeExpired();
    } catch (err) {
      quarantine = { at: iso(now()), code: errorCode(err) };
      log({ event: 'durable_spool_quarantined', code: quarantine.code });
    }
  }

  function startup() {
    provisionDir(config.spoolDir, fsync);
    // Re-sync the whole real ancestor chain on every start: heals a crash between an earlier mkdir and its
    // parent fsync, and covers ancestors created before startup (e.g. shared with the session dir).
    syncRealAncestors(config.spoolDir, fsync);
    lockFd = acquireOwnership(config.spoolDir, fsync, flockBin);
    try {
      for (const dir of Object.values(dirs)) {
        provisionDir(dir, fsync);
        syncDir(dir, fsync);
      }
      syncDir(config.spoolDir, fsync);
      const discarded = discardTmpFiles(dirs.tmp);
      if (discarded) log({ event: 'durable_spool_tmp_discarded', count: discarded });
      for (const record of loadRecords(dirs.inbox, INBOX_SCHEMA, 'envelope_id')) {
        inbox.set(record.envelope_id, { record, lease: null, deliveryCount: 0 });
      }
      for (const record of loadRecords(dirs.sends, LEDGER_SCHEMA, 'attempt_key')) ledger.set(record.attempt_key, record);
      loadAccountBinding();
      purgeExpired();
      maintenanceTimer = timers.setInterval(maintenance, maintenanceIntervalMs);
      maintenanceTimer?.unref?.();
    } catch (err) {
      closeSync(lockFd);
      lockFd = null;
      throw err;
    }
  }

  /**
   * Durable account binding. A bound spool may hold only its account's inbound records. An
   * unbound (pre-binding) spool may bind only to the one account its inbound records prove;
   * records of several accounts, or send records without any inbound evidence, fail closed.
   */
  function loadAccountBinding() {
    const records = loadRecords(dirs.account, ACCOUNT_SCHEMA, 'binding_id');
    const inboxAccounts = new Set([...inbox.values()].map((entry) => entry.record.instance_id));
    if (records.length > 1) throw new DurableStateError('spool is bound to more than one account');
    if (records.length === 1) {
      const { binding_id: id, account_jid: account } = records[0];
      if (canonicalAccountJid(account) !== account || accountKey(account) !== id) {
        throw new DurableStateError('corrupt account binding');
      }
      if ([...inboxAccounts].some((instanceId) => instanceId !== account)) {
        throw new DurableStateError('spool holds inbound records of another account');
      }
      binding = { id, account, record: records[0], durable: true }; // account/ was fsynced above, before loading
    } else if (inboxAccounts.size > 1) {
      throw new DurableStateError('unbound spool holds inbound records of more than one account');
    } else if (inboxAccounts.size === 1) {
      legacyInboxAccounts = inboxAccounts;
    } else if (ledger.size > 0) {
      throw new DurableStateError('unbound spool holds send records without account evidence');
    }
  }

  function close() {
    if (closed) return;
    closed = true;
    if (maintenanceTimer !== null) timers.clearInterval(maintenanceTimer);
    maintenanceTimer = null;
    if (retryBuffer.size) {
      log({ event: 'durable_inbound_dropped_on_close', count: retryBuffer.size });
      health.ingestDropped += retryBuffer.size;
      retryBuffer.clear();
    }
    if (lockFd !== null) closeSync(lockFd);
    lockFd = null;
  }

  function ingest(origin, msg) {
    const key = msg?.key ?? {};
    if (!isDurableGroup(key.remoteJid)) return { status: 'ignored', reason: 'not_durable_group' };
    if (key.fromMe !== false) return { status: 'ignored', reason: 'from_me' };
    if (!msg.message || typeof msg.message !== 'object') return { status: 'ignored', reason: 'no_message_content' };
    if (!matches(MESSAGE_ID_RE, key.id)) return { status: 'rejected', reason: 'invalid_message_id' };
    if (!matches(JID_RE, key.participant)) return { status: 'rejected', reason: 'invalid_participant' };
    // The envelope belongs to the account of the socket that EMITTED it (origin, captured by
    // routeUpsertBatch before the batch's first await), never to whichever socket is live now. The
    // live account must still be that account: after a reconnect to another account the old socket's
    // messages are refused, so they neither bind the spool to the new account nor reach its claims.
    // The account binding is made by persistInbound, so a failed binding keeps the message in the retry buffer.
    if (!INBOUND_ORIGINS.has(origin)) return { status: 'rejected', reason: 'origin_unknown' };
    const instanceId = origin.account;
    if (!instanceId) return { status: 'rejected', reason: 'account_unknown' };
    const live = liveAccount();
    if (live !== instanceId) return { status: 'rejected', reason: live ? 'account_changed' : 'account_unknown' };
    if (accountMismatch(instanceId)) return { status: 'rejected', reason: 'account_mismatch' };
    const messageContent = getMessageContent(msg);
    const content = extractContent(messageContent, config.maxTextChars);
    if (!content) return { status: 'ignored', reason: 'unsupported_content' };

    const envelopeId = inboxKey(instanceId, key.remoteJid, key.id);
    if (inbox.has(envelopeId)) return { status: 'duplicate', envelopeId };
    const buffered = retryBuffer.get(envelopeId);
    if (buffered) return persistInbound(buffered); // redelivery of a not-yet-durable message: retry now

    const contextInfo = getContextInfo(messageContent);
    const hasQuote = contextInfo.stanzaId !== undefined || contextInfo.participant !== undefined;
    const receivedAt = now();
    const record = {
      schema: INBOX_SCHEMA,
      envelope_id: envelopeId,
      state: 'pending',
      instance_id: instanceId,
      group_jid: key.remoteJid,
      message_id: key.id,
      participant: key.participant,
      push_name: optionalShortText(msg.pushName, MAX_PUSH_NAME_CHARS),
      received_at: iso(receivedAt),
      received_at_ms: receivedAt,
      provider_timestamp: toSafeInteger(msg.messageTimestamp),
      ...content,
      quoted: hasQuote ? {
        stanza_id: matches(MESSAGE_ID_RE, contextInfo.stanzaId) ? contextInfo.stanzaId : null,
        participant: matches(JID_RE, contextInfo.participant) ? contextInfo.participant : null,
      } : null,
    };
    return persistInbound({ record, attempts: 0, code: null });
  }

  /**
   * The only path that makes an inbound durable and claimable. A failed write is never
   * reported as persisted: the record waits in a bounded in-memory buffer (not durable,
   * not claimable) for the next maintenance ticks, then is dropped loudly. Bridge crash
   * or power loss while buffered loses it: Baileys does not redeliver (SDK boundary).
   * A failed account binding, or a live account that is no longer the record's, is such a
   * failure too: the record is retried only under the account it was received for.
   */
  function persistInbound(entry) {
    const envelopeId = entry.record.envelope_id;
    let code = closed ? 'closed' : (quarantine ? 'quarantined' : null);
    if (!code) code = inboundAccountError(entry.record.instance_id);
    if (!code) {
      try {
        writeJsonAtomic(dirs.inbox, dirs.tmp, `${envelopeId}.json`, entry.record, fsync);
        retryBuffer.delete(envelopeId);
        unsyncedInbox.delete(envelopeId);
        inbox.set(envelopeId, { record: entry.record, lease: null, deliveryCount: 0 });
        if (entry.attempts === 0) return { status: 'persisted', envelopeId };
        health.ingestRecovered += 1;
        return { status: 'persisted', envelopeId, recovered: true };
      } catch (err) {
        if (err.renamed) unsyncedInbox.add(envelopeId);
        code = errorCode(err);
      }
    }
    entry.attempts += 1;
    entry.code = code;
    health.ingestFailures += 1;
    health.lastIngestFailure = { at: iso(now()), code };
    const retained = code !== 'closed' && code !== 'quarantined' && entry.attempts < INGEST_RETRY.maxAttempts
      && (retryBuffer.has(envelopeId) || retryBuffer.size < INGEST_RETRY.maxBuffered);
    if (retained) {
      retryBuffer.set(envelopeId, entry);
    } else {
      retryBuffer.delete(envelopeId);
      health.ingestDropped += 1;
    }
    return { status: 'persist_failed', envelopeId, code, retained, attempts: entry.attempts };
  }

  /**
   * The live account must still be the record's account (checked first, so a retry never binds
   * the spool to another account), and the spool must be durably bound to it. Error code or null.
   */
  function inboundAccountError(account) {
    const live = liveAccount();
    if (live !== account) return live ? 'account_changed' : 'account_unknown';
    return accountGate().error ?? null;
  }

  function publicEnvelope(record) {
    const { state, acked_at: _ackedAt, acked_at_ms: _ackedAtMs, ack_token_sha256: _ackSha, ...envelope } = record;
    return envelope;
  }

  function claim(body) {
    const down = unavailableReply();
    if (down) return down;
    const gate = accountGate();
    if (gate.error) return accountReply(gate.error);
    const invalid = checkBodyKeys(body, ['limit']);
    if (invalid) return invalid;
    let limit = CLAIM_LIMIT.def;
    if (body.limit !== undefined) {
      if (!Number.isInteger(body.limit) || body.limit < 1 || body.limit > CLAIM_LIMIT.max) {
        return badRequest(`limit must be an integer between 1 and ${CLAIM_LIMIT.max}`);
      }
      limit = body.limit;
    }
    purgeExpired();
    const t = now();
    const candidates = [...inbox.values()]
      .filter((e) => e.record.state === 'pending' && groups.has(e.record.group_jid) && (!e.lease || e.lease.expiresAt <= t))
      .sort((a, b) => a.record.received_at_ms - b.record.received_at_ms
        || (a.record.envelope_id < b.record.envelope_id ? -1 : 1))
      .slice(0, limit);
    const leases = candidates.map((entry) => {
      const token = randomBytes(32).toString('hex');
      entry.lease = { tokenSha: sha256Hex(token), expiresAt: t + config.leaseMs };
      entry.deliveryCount += 1;
      return {
        envelope_id: entry.record.envelope_id,
        lease_token: token,
        lease_expires_at: iso(entry.lease.expiresAt),
        delivery_count: entry.deliveryCount,
        envelope: publicEnvelope(entry.record),
      };
    });
    return reply(200, { leases });
  }

  function ack(body) {
    const down = unavailableReply();
    if (down) return down;
    const gate = accountGate();
    if (gate.error) return accountReply(gate.error);
    const invalid = checkBodyKeys(body, ['envelope_id', 'lease_token']);
    if (invalid) return invalid;
    if (!matches(HEX64_RE, body.envelope_id) || !matches(HEX64_RE, body.lease_token)) {
      return badRequest('envelope_id and lease_token must be 64 lowercase hex chars');
    }
    const entry = inbox.get(body.envelope_id);
    if (!entry || !groups.has(entry.record.group_jid)) return reply(404, { error: 'unknown_envelope' });
    const tokenSha = sha256Hex(body.lease_token);
    if (entry.record.state === 'acked') {
      return sameHex(entry.record.ack_token_sha256, tokenSha)
        ? reply(200, { status: 'already_acked', envelope_id: body.envelope_id })
        : reply(409, { error: 'lease_invalid' });
    }
    if (!entry.lease || !sameHex(entry.lease.tokenSha, tokenSha)) return reply(409, { error: 'lease_invalid' });
    const t = now();
    if (entry.lease.expiresAt <= t) return reply(409, { error: 'lease_expired' });
    const acked = { ...entry.record, state: 'acked', acked_at: iso(t), acked_at_ms: t, ack_token_sha256: tokenSha };
    writeJsonAtomic(dirs.inbox, dirs.tmp, `${body.envelope_id}.json`, acked, fsync);
    entry.record = acked;
    entry.lease = null;
    return reply(200, { status: 'acked', envelope_id: body.envelope_id });
  }

  const acceptedIds = (record) => record.chunks.filter((c) => c.state === 'accepted').map((c) => c.message_id);
  const confirmedReply = (record, replayed) => {
    const ids = acceptedIds(record);
    return reply(200, {
      success: true, attempt_id: record.attempt_id, state: 'confirmed', messageId: ids[ids.length - 1], messageIds: ids, replayed,
    });
  };
  const unknownReply = (status, record) => reply(status, {
    success: false, error: 'delivery_unknown', state: 'delivery_unknown', attempt_id: record.attempt_id, messageIds: acceptedIds(record),
  });

  function markUnknown(key, record, reason) {
    const t = now();
    const next = { ...record, state: 'unknown', error: reason, updated_at: iso(t), updated_at_ms: t };
    ledger.set(key, next);
    try {
      writeJsonAtomic(dirs.sends, dirs.tmp, `${key}.json`, next, fsync);
    } catch (err) {
      // Disk keeps the earlier 'intent' record, which also reads as delivery_unknown.
      log({ event: 'durable_send_ledger_write_failed', attempt_id: record.attempt_id, phase: 'mark_unknown', code: errorCode(err) });
    }
    return unknownReply(502, next);
  }

  async function send(body) {
    const down = unavailableReply();
    if (down) return down;
    const gate = accountGate();
    if (gate.error) return accountReply(gate.error);
    const invalid = checkBodyKeys(body, ['chatId', 'message', 'replyTo', 'attempt_id']);
    if (invalid) return invalid;
    const { chatId, message, replyTo, attempt_id: attemptId } = body;
    if (!matches(ATTEMPT_ID_RE, attemptId)) return badRequest('attempt_id must match [A-Za-z0-9._:-]{8,128}');
    if (typeof message !== 'string' || message.length < 1 || message.length > MAX_OUTBOUND_CHARS) {
      return badRequest(`message must be a non-empty string up to ${MAX_OUTBOUND_CHARS} chars`);
    }
    if (replyTo !== undefined && replyTo !== null && !matches(MESSAGE_ID_RE, replyTo)) return badRequest('replyTo is not a message id');
    if (!isDurableGroup(chatId)) return reply(403, { error: 'chat_not_configured' });

    const key = attemptKey(attemptId);
    if (inFlight.has(key)) return reply(409, { error: 'attempt_in_progress', attempt_id: attemptId });
    const requestSha = sha256Hex(JSON.stringify([chatId, message, replyTo ?? null]));
    const existing = ledger.get(key);
    if (existing) {
      if (existing.request_sha256 !== requestSha) return reply(409, { error: 'attempt_conflict', attempt_id: attemptId });
      if (existing.state !== 'confirmed') return unknownReply(409, existing);
      return confirmedReply(existing, true);
    }

    let quoted;
    let replyEnvelopeId = null;
    if (replyTo) {
      const target = inbox.get(inboxKey(gate.account, chatId, replyTo));
      if (!target) return reply(422, { error: 'reply_target_not_found', attempt_id: attemptId });
      if (target.record.content_type !== 'text') return reply(422, { error: 'reply_target_not_quotable', attempt_id: attemptId });
      replyEnvelopeId = target.record.envelope_id;
      quoted = {
        key: { remoteJid: chatId, fromMe: false, id: target.record.message_id, participant: target.record.participant },
        message: { conversation: target.record.text },
      };
    }
    if (!deps.isConnected()) return reply(503, { error: 'not_connected', attempt_id: attemptId });
    const chunks = deps.prepareChunks(message);
    if (!Array.isArray(chunks) || chunks.length < 1 || !chunks.every((c) => typeof c === 'string' && c)) {
      return badRequest('message produced no sendable chunks');
    }

    inFlight.add(key);
    try {
      const t0 = now();
      let record = {
        schema: LEDGER_SCHEMA,
        attempt_key: key,
        attempt_id: attemptId,
        chat_jid: chatId,
        request_sha256: requestSha,
        reply_to_message_id: replyTo ?? null,
        reply_to_envelope_id: replyEnvelopeId,
        state: 'intent',
        chunks_total: chunks.length,
        chunks: chunks.map((_, index) => ({ index, state: 'pending', message_id: null })),
        error: null,
        created_at: iso(t0),
        created_at_ms: t0,
        updated_at: iso(t0),
        updated_at_ms: t0,
      };
      try {
        writeJsonAtomic(dirs.sends, dirs.tmp, `${key}.json`, record, fsync);
      } catch (err) {
        // Renamed but not known durable: mirror disk (intent => delivery_unknown on retry, as after a restart).
        if (err.renamed) ledger.set(key, record);
        log({ event: 'durable_send_ledger_write_failed', attempt_id: attemptId, phase: 'intent', code: errorCode(err) });
        return reply(500, { error: 'ledger_persist_failed', attempt_id: attemptId });
      }
      ledger.set(key, record);

      for (let i = 0; i < chunks.length; i += 1) {
        // The socket may be re-paired to another account while chunks are paced: never send from it.
        if (liveAccount() !== gate.account) return markUnknown(key, record, 'account_changed');
        let sent;
        try {
          sent = await deps.sendMessage(chatId, { text: chunks[i] }, i === 0 && quoted ? { quoted } : undefined);
        } catch (err) {
          return markUnknown(key, record, `send_error: ${errorText(err)}`);
        }
        const providerId = sent?.key?.id;
        if (typeof providerId !== 'string' || !providerId) return markUnknown(key, record, 'no_provider_message_id');
        deps.onSent?.(sent);
        const t = now();
        const updatedChunks = record.chunks.map((c) => (c.index === i ? { ...c, state: 'accepted', message_id: providerId } : c));
        const next = {
          ...record,
          chunks: updatedChunks,
          state: updatedChunks.every((c) => c.state === 'accepted') ? 'confirmed' : 'intent',
          updated_at: iso(t),
          updated_at_ms: t,
        };
        try {
          writeJsonAtomic(dirs.sends, dirs.tmp, `${key}.json`, next, fsync);
        } catch (err) {
          log({ event: 'durable_send_ledger_write_failed', attempt_id: attemptId, phase: 'accepted', code: errorCode(err) });
          const known = { ...next, state: 'unknown', error: `ledger_write_failed: ${errorText(err)}` };
          ledger.set(key, known);
          return unknownReply(502, known);
        }
        record = next;
        ledger.set(key, record);
        if (i < chunks.length - 1) await deps.betweenChunks();
      }
      return confirmedReply(record, false);
    } finally {
      inFlight.delete(key);
    }
  }

  function receipt(attemptId) {
    const down = unavailableReply();
    if (down) return down;
    const gate = accountGate();
    if (gate.error) return accountReply(gate.error);
    if (!matches(ATTEMPT_ID_RE, attemptId)) return badRequest('attempt_id must match [A-Za-z0-9._:-]{8,128}');
    const key = attemptKey(attemptId);
    const record = ledger.get(key);
    if (!record) return reply(404, { error: 'unknown_attempt' });
    let state = 'delivery_unknown';
    if (inFlight.has(key)) state = 'in_progress';
    else if (record.state === 'confirmed') state = 'confirmed';
    return reply(200, {
      attempt_id: record.attempt_id,
      chat_jid: record.chat_jid,
      state,
      ledger_state: record.state,
      message_ids: acceptedIds(record),
      chunks_total: record.chunks_total,
      chunks: record.chunks,
      reply_to_message_id: record.reply_to_message_id,
      reply_to_envelope_id: record.reply_to_envelope_id,
      error: record.error,
      created_at: record.created_at,
      updated_at: record.updated_at,
    });
  }

  function stats() {
    const t = now();
    const live = liveAccount();
    const mismatch = live !== null && accountMismatch(live);
    const out = {
      enabled: true, groups: groups.size, inbox_pending: 0, inbox_leased: 0, inbox_acked: 0, inbox_unconfigured_group: 0,
      sends_confirmed: 0, sends_unknown: 0, sends_in_flight: inFlight.size,
      degraded: closed || quarantine !== null || retryBuffer.size > 0 || health.ingestDropped > 0 || mismatch,
      quarantined: quarantine !== null,
      account_bound: binding?.durable === true,
      account_mismatch: mismatch,
      closed,
      ingest_failures: health.ingestFailures,
      ingest_retry_buffered: retryBuffer.size,
      ingest_recovered: health.ingestRecovered,
      ingest_dropped: health.ingestDropped,
      last_ingest_failure: health.lastIngestFailure,
    };
    for (const { record, lease } of inbox.values()) {
      if (!groups.has(record.group_jid)) out.inbox_unconfigured_group += 1;
      else if (record.state === 'acked') out.inbox_acked += 1;
      else if (lease && lease.expiresAt > t) out.inbox_leased += 1;
      else out.inbox_pending += 1;
    }
    for (const [key, record] of ledger) {
      if (record.state === 'confirmed') out.sends_confirmed += 1;
      else if (!inFlight.has(key)) out.sends_unknown += 1;
    }
    return out;
  }

  startup();
  return Object.freeze({ isDurableGroup, ingest, claim, ack, send, receipt, stats, close });
}

// ------------------------------------------------- emitting socket identity (messages.upsert)

const INBOUND_ORIGINS = new WeakSet();

/**
 * Immutable technical identity of the socket that EMITTED a messages.upsert batch: its account
 * (Baileys `socket.user.id`, device suffix removed; null if unknown), read now, synchronously.
 * ingest accepts only origins made here; nothing from HTTP or a model can produce one.
 */
export function captureInboundOrigin(socket) {
  const origin = Object.freeze({ account: canonicalAccountJid(socket?.user?.id) });
  INBOUND_ORIGINS.add(origin);
  return origin;
}

/**
 * bridge.js messages.upsert loop: `for (const msg of routeUpsertBatch(durable, emitter, messages, report))`.
 * Synchronously, before returning: captures the emitter's origin once, then ingests every durable-group
 * message of the batch under it, in order, passing each result to report(). Only then does it return
 * the other messages, in their relative order, for the legacy loop body, which may await media
 * downloads. So no durable message waits in memory behind a legacy await, and a reconnect during
 * such an await cannot change whose messages they are.
 */
export function routeUpsertBatch(transport, emitter, messages, report) {
  const origin = captureInboundOrigin(emitter);
  const legacy = [];
  for (const msg of messages) {
    if (!transport?.isDurableGroup(msg?.key?.remoteJid)) {
      legacy.push(msg);
      continue;
    }
    let result;
    try {
      result = transport.ingest(origin, msg);
    } catch (err) {
      result = { status: 'error', code: typeof err?.code === 'string' ? err.code : (err?.name ?? 'Error') };
    }
    report(result);
  }
  return legacy;
}

const defaultHttpLog = (entry) => console.error(JSON.stringify(entry));
const legacyRouteFor = (method, urlPath) => (method === 'POST'
  ? LEGACY_MESSAGE_ROUTES.find((route) => LEGACY_ROUTE_RES.get(route).test(urlPath)) : undefined);

/** Forms a caller could use to reach a configured group through a legacy route (Baileys is lenient). */
function chatIdCandidates(chatId) {
  const raw = String(chatId ?? '');
  return [raw, raw.trim().toLowerCase().replace(/:[0-9]+(?=@)/, '')];
}

/**
 * Guard for a legacy message route. null => legacy handler runs unchanged (chats outside
 * the durable whitelist). Configured groups are reachable only through the ledgered
 * attempt send, so a task started before activation cannot answer outside the ledger.
 */
async function guardLegacyMessageRoute(transport, route, body, log) {
  const hasAttempt = body !== null && typeof body === 'object' && Object.hasOwn(body, 'attempt_id');
  if (route === '/send' && hasAttempt) {
    return transport ? transport.send(body) : reply(400, { error: 'durable_transport_disabled' });
  }
  if (hasAttempt) return reply(400, { error: 'attempt_id_not_supported', route }); // only /send is ledgered
  const candidates = chatIdCandidates(body?.chatId);
  if (transport && candidates.some((candidate) => transport.isDurableGroup(candidate))) {
    log({ event: 'durable_legacy_route_denied', route });
    return reply(403, { error: 'legacy_route_disabled_for_durable_group', route });
  }
  return null;
}

/**
 * Route a bridge HTTP request to the durable transport. Returns null when the request
 * belongs to the legacy handlers. `route` is the legacy route Express already matched
 * (installDurableRoutes); without it the Express-equivalent regexes classify the path.
 * `transport` is null when the durable transport is disabled.
 */
export async function routeDurableRequest(transport, { method, path: urlPath, body, route }, log = defaultHttpLog) {
  const legacyRoute = route ?? legacyRouteFor(method, urlPath);
  if (!legacyRoute && !DURABLE_NS_RE.test(urlPath)) return null;
  try {
    if (legacyRoute) return await guardLegacyMessageRoute(transport, legacyRoute, body, log);
    if (!transport) return reply(404, { error: 'durable_transport_disabled' });
    if (method === 'POST' && CLAIM_PATH_RE.test(urlPath)) return transport.claim(body);
    if (method === 'POST' && ACK_PATH_RE.test(urlPath)) return transport.ack(body);
    const receiptMatch = method === 'GET' ? RECEIPT_PATH_RE.exec(urlPath) : null;
    if (receiptMatch) {
      let attemptId;
      try {
        attemptId = decodeURIComponent(receiptMatch[1]);
      } catch {
        return badRequest('attempt_id is not valid percent-encoding');
      }
      return transport.receipt(attemptId);
    }
    return reply(404, { error: 'not_found' });
  } catch (err) {
    log({ event: 'durable_http_error', method, route: legacyRoute ?? 'durable', code: errorCode(err) });
    return reply(500, { error: 'durable_internal_error' });
  }
}

/**
 * Wire the durable routes into the bridge's Express app. MUST run before the legacy
 * handlers are registered: each guard uses the SAME path string on the same router,
 * so Express's own matcher (case-insensitive, optional trailing slash) sends every
 * variant that would reach a legacy handler through its guard first.
 */
export function installDurableRoutes(app, transport, log = defaultHttpLog) {
  const handler = (route) => async (req, res, next) => {
    try {
      const result = await routeDurableRequest(transport, { method: req.method, path: req.path, body: req.body, route }, log);
      if (!result) return next();
      return res.status(result.status).json(result.body);
    } catch (err) {
      return next(err);
    }
  };
  for (const route of LEGACY_MESSAGE_ROUTES) app.post(route, handler(route));
  app.all(['/durable', '/durable/*'], handler(undefined));
}
