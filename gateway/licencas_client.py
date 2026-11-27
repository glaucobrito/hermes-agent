"""Cliente HTTP tipado da fachada Edge ``licencas-api`` (unidade A2-client).

Atende B-EXECUTION-CONTRACTS §5 e RPC-COMPLEMENTO-CONTRATUAL §1.4/§2: sete
operações, corpo ``{version, operation, context, payload}`` assinado com
HMAC-SHA256, tipos fechados na entrada e na saída.

Regras deste módulo:

* Um pedido por chamada. Nenhum retry, nenhuma tentativa nova por conta própria:
  cada erro diz, em ``action``, o que o chamador pode fazer.
* O escopo do contexto vem só da configuração; os valores de ator vêm do
  transporte trusted, passados pelo chamador. Nada aqui lê conversa ou modelo.
* O log tem operação, status, código e request_id. Nunca cabeçalhos, corpo,
  texto, token, JID, telefone, IDs de mensagem ou hash.
* Importar não faz nada: sem leitura de ambiente, arquivo ou rede. A
  configuração só é lida quando ``load_licencas_config`` é chamada.
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import ClassVar, Generic, NoReturn, TypeVar
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")
E = TypeVar("E", bound=enum.Enum)

# ---------------------------------------------------------------- constantes do contrato §5

SIGNATURE_DOMAIN = "licencas/v1"
MAX_BODY_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
NONCE_BYTES = 32
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 15.0
WRITE_TIMEOUT_S = 5.0
POOL_TIMEOUT_S = 5.0
_LOOPBACK_TEST_HOST = "127.0.0.1"

#: Operação HTTP -> tipo de contexto exigido pela Edge (mapa literal do contrato).
OPERATIONS: Mapping[str, str] = {
    "ingestEnvelope": "member",
    "recordReply": "member",
    "queryFollowups": "member",
    "listCaptured": "worker",
    "claimDue": "worker",
    "prepareIntent": "worker",
    "confirmReceipt": "worker",
}

_ENV_TO_FIELD: Mapping[str, str] = {
    "LICENCAS_API_URL": "endpoint",
    "LICENCAS_API_SIGNED_PATHNAME": "signed_pathname",
    "LICENCAS_CAPABILITY": "capability",
    "LICENCAS_HMAC_KEY_B64": "hmac_key",
    "LICENCAS_KEY_ID": "key_id",
    "LICENCAS_PRINCIPAL_ID": "principal_id",
    "LICENCAS_INSTANCE_ID": "instance_id",
    "LICENCAS_POLICY_ID": "policy_id",
    "LICENCAS_GROUP_JID": "group_jid",
    "LICENCAS_POLICY_EPOCH": "policy_epoch",
    "LICENCAS_ACCOUNT_BINDING_HASH": "account_binding_hash",
}
#: Variáveis de ambiente obrigatórias (todas; não há valor padrão para nenhuma).
LICENCAS_ENV_NAMES: tuple[str, ...] = tuple(_ENV_TO_FIELD)

# Formatos fechados, cópia dos da Edge (contract.ts); sempre usados com fullmatch.
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TOKEN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_GROUP_JID = re.compile(r"[0-9]{6,32}(?:-[0-9]{6,20})?@g\.us")
_ACCOUNT_JID = re.compile(r"[A-Za-z0-9._+-]{1,96}@[a-z0-9.-]{1,32}")
_CAPABILITY = re.compile(r"[A-Za-z0-9._~+/=-]{32,512}")
_JWT_SHAPE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_EPOCH = re.compile(r"[1-9][0-9]{0,15}")
_PATHNAME = re.compile(r"(?:/[A-Za-z0-9._~-]+)+")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[1-9][0-9]{0,4})?")
_ACTOR_JID = re.compile(r"[A-Za-z0-9._:+-]{1,96}@(?:s\.whatsapp\.net|lid)")
_OWNER_JID = re.compile(r"[0-9]{5,40}(?::[0-9]{1,5})?@(?:s\.whatsapp\.net|lid)")
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_RECEIPT_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_E164 = re.compile(r"\+[1-9][0-9]{7,14}")
_ATTESTATION_SOURCE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CURSOR = re.compile(r"[A-Za-z0-9._~-]{1,512}")
_CORRELATION_TOKEN = re.compile(r"[A-Za-z0-9_-]{22,128}")
_PROMPT_TOKEN = re.compile(r"[0-9a-f]{40}")
_TIMEZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,3}")
_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}"
)
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{2,63}")
_INSTANT = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.[0-9]{1,6})?)?"
    r"(?:Z|[+-]([0-9]{2}):([0-9]{2}))"
)
_SECONDS_INSTANT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_UTC_MICROS = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"
)
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_ERROR_STATUSES = frozenset({400, 401, 403, 404, 409, 413, 429, 503})

# ---------------------------------------------------------------- erros tipados


class LicencasAction(enum.Enum):
    """O que o chamador pode fazer depois de um erro."""

    #: 503 busy: nada foi gravado. Repetir a MESMA tentativa (mesmas identidades de domínio).
    RETRY_SAME_ATTEMPT = "retry_same_attempt"
    #: 409 replay_unconfirmed: chamar de novo; o cliente gera timestamp/nonce/request_id novos.
    RESEND_FRESH_AUTH = "resend_fresh_auth"
    #: 429 ou conexão não estabelecida: recuar antes de chamar de novo.
    BACK_OFF = "back_off"
    #: 400, 401, 403, 404, 413 e demais 409, ou recusa local: repetir igual não muda o resultado.
    DO_NOT_RETRY = "do_not_retry"
    #: Demais 503: fachada indisponível. Não assumir efeito nem ausência de efeito.
    UNAVAILABLE = "unavailable"
    #: Timeout/erro de rede depois de enviar, ou resposta fora do contrato: resultado desconhecido.
    OUTCOME_UNKNOWN = "outcome_unknown"


class LicencasError(Exception):
    """Base. A mensagem só carrega operação, status, código, request_id e caminho técnico."""

    action: ClassVar[LicencasAction]

    def __init__(
        self,
        *,
        operation: str | None,
        request_id: str | None,
        status: int | None,
        code: str,
        detail: str | None = None,
    ) -> None:
        suffix = "" if detail is None else f" detail={detail}"
        super().__init__(
            f"licencas operation={operation} status={status} code={code} request_id={request_id}{suffix}"
        )
        self.operation = operation
        self.request_id = request_id
        self.status = status
        self.code = code
        self.detail = detail


class LicencasConfigError(LicencasError):
    """Configuração ausente ou inválida: o cliente não é criado. ``field`` nomeia o campo, nunca o valor."""

    action = LicencasAction.DO_NOT_RETRY

    def __init__(self, field_name: str) -> None:
        super().__init__(
            operation=None,
            request_id=None,
            status=None,
            code="config_invalid",
            detail=field_name,
        )
        self.field = field_name


class LicencasRequestError(LicencasError):
    """Recusa local, antes de qualquer envio (entrada fora do codec, JSON inválido, corpo > 16 KiB)."""

    action = LicencasAction.DO_NOT_RETRY


class LicencasBusyError(LicencasError):
    """503 ``busy``: contenda de lock no banco, sem efeito."""

    action = LicencasAction.RETRY_SAME_ATTEMPT


class LicencasReplayUnconfirmedError(LicencasError):
    """409 ``replay_unconfirmed``: pedido idêntico já visto e sem recibo persistido."""

    action = LicencasAction.RESEND_FRESH_AUTH


class LicencasRateLimitedError(LicencasError):
    """429: limite por principal."""

    action = LicencasAction.BACK_OFF


class LicencasRejectedError(LicencasError):
    """400, 401, 403, 404, 413 e 409 de domínio: ``code`` diz o motivo fechado."""

    action = LicencasAction.DO_NOT_RETRY


class LicencasUnavailableError(LicencasError):
    """503 diferente de ``busy``. Alguns códigos saem depois da RPC de domínio: efeito não garantido."""

    action = LicencasAction.UNAVAILABLE


class LicencasNotSentError(LicencasError):
    """A conexão não foi estabelecida: nenhum byte do pedido saiu."""

    action = LicencasAction.BACK_OFF


class LicencasOutcomeUnknownError(LicencasError):
    """Timeout ou erro de rede depois de enviar: o efeito pode ou não ter acontecido."""

    action = LicencasAction.OUTCOME_UNKNOWN


class LicencasResponseError(LicencasOutcomeUnknownError):
    """Resposta fora do contrato (forma, tipo, status, redirect, tamanho). Nunca vira sucesso."""


class _Refusal(Exception):
    """Interno: entrada recusada antes do envio. ``path`` é caminho técnico, nunca valor."""

    def __init__(self, code: str, path: str) -> None:
        super().__init__(code)
        self.code = code
        self.path = path


class _Malformed(Exception):
    """Interno: resposta fora do DTO. ``path`` é caminho técnico, nunca valor."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


_Fail = Callable[[str], NoReturn]


def _refuse(path: str) -> NoReturn:
    raise _Refusal("invalid_input", path)


def _malformed(path: str) -> NoReturn:
    raise _Malformed(path)


# ---------------------------------------------------------------- validação estrita


def _fields(
    value: object,
    path: str,
    fail: _Fail,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> dict[str, object]:
    if not isinstance(value, dict):
        fail(path)
    for key in value:
        if key not in required and key not in optional:
            fail(f"{path}:unexpected_key")
    for key in required:
        if key not in value:
            fail(f"{path}:missing_key")
    return value


def _text(
    value: object, path: str, fail: _Fail, pattern: re.Pattern[str] | None = None
) -> str:
    if not isinstance(value, str) or (
        pattern is not None and pattern.fullmatch(value) is None
    ):
        fail(path)
    return value


def _optional_text(
    value: object, path: str, fail: _Fail, pattern: re.Pattern[str] | None = None
) -> str | None:
    return None if value is None else _text(value, path, fail, pattern)


def _integer(value: object, path: str, fail: _Fail, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        fail(path)
    return value


def _flag(value: object, path: str, fail: _Fail) -> bool:
    if type(value) is not bool:
        fail(path)
    return value


def _member(value: object, path: str, fail: _Fail, kind: type[E]) -> E:
    """Entrada: exige o próprio enum (texto cru é recusado)."""
    if not isinstance(value, kind):
        fail(path)
    return value


def _parse_enum(
    value: object,
    path: str,
    fail: _Fail,
    kind: type[E],
    allowed: tuple[E, ...] | None = None,
) -> E:
    """Saída: texto do contrato -> enum fechado."""
    for item in kind if allowed is None else allowed:
        if item.value == value and isinstance(value, str):
            return item
    fail(path)


def _sequence(
    value: object, path: str, fail: _Fail, low: int, high: int
) -> list[object]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        fail(path)
    return value


def _unique_ids(
    value: object, path: str, fail: _Fail, pattern: re.Pattern[str], low: int, high: int
) -> tuple[str, ...]:
    items = tuple(
        _text(item, path, fail, pattern)
        for item in _sequence(value, path, fail, low, high)
    )
    if len(set(items)) != len(items):
        fail(path)
    return items


def _instant(
    value: object, path: str, fail: _Fail, shape: re.Pattern[str] | None = None
) -> str:
    """ISO-8601 com fuso explícito e data de calendário existente (mesma gramática da Edge)."""
    raw = _text(value, path, fail, shape)
    match = _INSTANT.fullmatch(raw)
    if match is None:
        fail(path)
    year, month, day, hour, minute = (int(match[index]) for index in range(1, 6))
    second, offset_hours, offset_minutes = (
        int(match[index] or "0") for index in (6, 7, 8)
    )
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    if (
        year < 1
        or not 1 <= month <= 12
        or hour > 23
        or minute > 59
        or second > 59
        or offset_hours > 15
        or offset_minutes > 59
    ):
        fail(path)
    if not 1 <= day <= (29 if month == 2 and leap else _DAYS_IN_MONTH[month - 1]):
        fail(path)
    return raw


def _optional_instant(value: object, path: str, fail: _Fail) -> str | None:
    return None if value is None else _instant(value, path, fail)


def _display_name(value: object, path: str) -> str | None:
    if value is None:
        return None
    name = _text(value, path, _refuse)
    if not 1 <= len(name) <= 256 or any(
        unicodedata.category(char) == "Cc" for char in name
    ):
        _refuse(path)
    return name


# ---------------------------------------------------------------- enums do contrato


class OperationalStatus(enum.Enum):
    NEEDS_UPDATE = "needs_update"
    IN_PROGRESS = "in_progress"
    WAITING_THIRD_PARTY = "waiting_third_party"
    WAITING_PUBLIC_AGENCY = "waiting_public_agency"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    NO_ACTION_NEEDED = "no_action_needed"


class RiskLevel(enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AgendaEffect(enum.Enum):
    UNCHANGED = "unchanged"
    CANCELLED = "cancelled"
    SCHEDULED = "scheduled"
    RESCHEDULED = "rescheduled"


class IdentityState(enum.Enum):
    PENDING = "pending"
    TRANSPORT_ATTESTED = "transport_attested"


class ProcessingState(enum.Enum):
    CAPTURED = "captured"
    APPLIED = "applied"
    NEEDS_CLARIFICATION = "needs_clarification"


class ClarificationReason(enum.Enum):
    TEXT_TRUNCATED = "text_truncated"
    MISSING_CORRELATION = "missing_correlation"
    UNRECOGNIZED_COMMAND = "unrecognized_command"
    CORRELATION_INVALID = "correlation_invalid"
    CORRELATION_STALE = "correlation_stale"
    PATCH_REJECTED = "patch_rejected"


class DeliveryUnknownReason(enum.Enum):
    SEND_TIMEOUT = "send_timeout"
    SEND_DEADLINE_EXCEEDED = "send_deadline_exceeded"
    TRANSPORT_ERROR = "transport_error"
    PARTIAL_SEND = "partial_send"
    RECEIPT_UNAVAILABLE = "receipt_unavailable"
    GATEWAY_RESTART = "gateway_restart"


class IngestOutcome(enum.Enum):
    CAPTURED = "captured"
    ALREADY_CAPTURED = "already_captured"


class ApplyOutcome(enum.Enum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"


class ClarificationOutcome(enum.Enum):
    NEEDS_CLARIFICATION = "needs_clarification"
    ALREADY_NEEDS_CLARIFICATION = "already_needs_clarification"


class ReceiptOutcome(enum.Enum):
    CONFIRMED = "confirmed"
    ALREADY_CONFIRMED = "already_confirmed"
    RECORDED_UNKNOWN = "recorded_unknown"


class ReceiptState(enum.Enum):
    CONFIRMED = "confirmed"
    DELIVERY_UNKNOWN = "delivery_unknown"


class Unset(enum.Enum):
    """Chave do patch ausente (diferente de ``None``, que grava nulo)."""

    UNSET = "unset"


UNSET = Unset.UNSET

# ---------------------------------------------------------------- configuração (tipo fechado)


@dataclass(frozen=True, slots=True)
class LicencasClientConfig:
    """Configuração trusted. Todos os campos são obrigatórios e validados na construção."""

    endpoint: str
    signed_pathname: str
    capability: str = field(repr=False)
    hmac_key: bytes = field(repr=False)
    key_id: str
    principal_id: str
    instance_id: str
    policy_id: str
    group_jid: str
    policy_epoch: int
    account_binding_hash: str

    def __post_init__(self) -> None:
        def need(name: str, pattern: re.Pattern[str]) -> str:
            value = getattr(self, name)
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise LicencasConfigError(name)
            return value

        pathname = need("signed_pathname", _PATHNAME)
        _check_endpoint(self.endpoint, pathname)
        capability = need("capability", _CAPABILITY)
        # A capability não é credencial de plataforma: forma de JWT ou de chave de API é recusada.
        if (
            capability.startswith(("eyJ", "sb_"))
            or _JWT_SHAPE.fullmatch(capability) is not None
        ):
            raise LicencasConfigError("capability")
        if not isinstance(self.hmac_key, bytes) or not 32 <= len(self.hmac_key) <= 128:
            raise LicencasConfigError("hmac_key")
        need("key_id", _TOKEN_ID)
        need("principal_id", _TOKEN_ID)
        need("instance_id", _ACCOUNT_JID)
        need("policy_id", _UUID)
        need("group_jid", _GROUP_JID)
        if (
            type(self.policy_epoch) is not int
            or not 1 <= self.policy_epoch <= MAX_SAFE_INTEGER
        ):
            raise LicencasConfigError("policy_epoch")
        need("account_binding_hash", _HEX64)


def _check_endpoint(endpoint: object, signed_pathname: str) -> None:
    """Endpoint TLS exato: esquema seguro, host, caminho terminando no pathname assinado; nada além disso."""
    if not isinstance(endpoint, str):
        raise LicencasConfigError("endpoint")
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise LicencasConfigError("endpoint") from None
    exact = f"{parts.scheme}://{parts.netloc}{parts.path}"
    clean = (
        parts.scheme == "https"
        and port != 0
        and exact == endpoint
        and _HOST.fullmatch(parts.netloc) is not None
        and _PATHNAME.fullmatch(parts.path) is not None
        and parts.path.endswith(signed_pathname)
    )
    if not clean:
        raise LicencasConfigError("endpoint")


def load_licencas_config(
    read: Callable[[str], str | None] = os.environ.get,
) -> LicencasClientConfig:
    """Lê as 11 variáveis; qualquer ausência ou forma inválida levanta ``LicencasConfigError`` com o nome dela."""
    values: dict[str, str] = {}
    for name in LICENCAS_ENV_NAMES:
        value = read(name)
        if value is None or value == "":
            raise LicencasConfigError(name)
        values[name] = value
    key_text = values["LICENCAS_HMAC_KEY_B64"]
    try:
        hmac_key = (
            base64.b64decode(key_text, validate=True)
            if _BASE64.fullmatch(key_text)
            else b""
        )
    except binascii.Error:
        raise LicencasConfigError("LICENCAS_HMAC_KEY_B64") from None
    if base64.b64encode(hmac_key).decode("ascii") != key_text:
        raise LicencasConfigError("LICENCAS_HMAC_KEY_B64")
    if _EPOCH.fullmatch(values["LICENCAS_POLICY_EPOCH"]) is None:
        raise LicencasConfigError("LICENCAS_POLICY_EPOCH")
    try:
        return LicencasClientConfig(
            endpoint=values["LICENCAS_API_URL"],
            signed_pathname=values["LICENCAS_API_SIGNED_PATHNAME"],
            capability=values["LICENCAS_CAPABILITY"],
            hmac_key=hmac_key,
            key_id=values["LICENCAS_KEY_ID"],
            principal_id=values["LICENCAS_PRINCIPAL_ID"],
            instance_id=values["LICENCAS_INSTANCE_ID"],
            policy_id=values["LICENCAS_POLICY_ID"],
            group_jid=values["LICENCAS_GROUP_JID"],
            policy_epoch=int(values["LICENCAS_POLICY_EPOCH"]),
            account_binding_hash=values["LICENCAS_ACCOUNT_BINDING_HASH"],
        )
    except LicencasConfigError as error:
        by_field = {attribute: name for name, attribute in _ENV_TO_FIELD.items()}
        raise LicencasConfigError(by_field[error.field]) from None


# ---------------------------------------------------------------- entradas (transporte trusted)


@dataclass(frozen=True, slots=True)
class MembershipAttestation:
    """Atestado do produtor trusted (bridge) de que o ator é participante ATUAL do grupo."""

    is_current_member: bool
    checked_at: str


@dataclass(frozen=True, slots=True)
class PhoneAttestation:
    e164: str
    source: str
    attested_at: str


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Contexto de ator (ingestEnvelope, recordReply, queryFollowups)."""

    actor_jid: str
    actor_display_name: str | None
    wa_message_id: str
    envelope_id: str
    message_timestamp: str
    observed_at: str
    membership: MembershipAttestation
    phone: PhoneAttestation | None


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """Contexto de worker (listCaptured e as três de cobrança): sem ator, mensagem ou atestado."""

    observed_at: str


@dataclass(frozen=True, slots=True)
class AttachmentMetadata:
    media_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class InboundEnvelope:
    """Parte do envelope A1 que não está no contexto. O texto segue byte a byte, sem trim."""

    received_at: str
    text: str = field(repr=False)
    text_truncated: bool
    quote_message_id: str | None
    quote_actor_jid: str | None
    attachment_metadata: AttachmentMetadata | None


@dataclass(frozen=True, slots=True)
class ReplyCorrelation:
    """Pelo menos um dos dois; ``None`` omite a chave."""

    quote_message_id: str | None
    token: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class NextContactKeep:
    pass


@dataclass(frozen=True, slots=True)
class NextContactCancel:
    pass


@dataclass(frozen=True, slots=True)
class NextContactSchedule:
    """Agendamento via WhatsApp: a proveniência é sempre ``owner_agreed`` (única aceita pela Edge)."""

    due_at: str
    timezone: str


@dataclass(frozen=True, slots=True)
class ReplyPatch:
    """Só as chaves diferentes de ``UNSET`` são enviadas; pelo menos uma é obrigatória."""

    operational_status: OperationalStatus | Unset = UNSET
    next_action: str | None | Unset = UNSET
    next_action_due_at: str | None | Unset = UNSET
    blocker_summary: str | None | Unset = UNSET
    evidence_summary: str | None | Unset = UNSET
    next_contact: NextContactKeep | NextContactCancel | NextContactSchedule | Unset = (
        UNSET
    )


@dataclass(frozen=True, slots=True)
class ApplyReply:
    correlation: ReplyCorrelation
    patch: ReplyPatch


@dataclass(frozen=True, slots=True)
class NeedsClarification:
    reason: ClarificationReason


@dataclass(frozen=True, slots=True)
class FollowupFilter:
    """``None`` = filtro ausente (a chave não é enviada)."""

    document_id: str | None = None
    operational_status: OperationalStatus | None = None
    risk_level: RiskLevel | None = None


@dataclass(frozen=True, slots=True)
class AttemptRef:
    job_id: str
    attempt_id: str
    fence: int


@dataclass(frozen=True, slots=True)
class OwnerMembership:
    """Atestado de participação do responsável (D4). Vai no payload do prepareIntent, nunca no contexto."""

    owner_jid: str
    is_current_member: bool
    checked_at: str


@dataclass(frozen=True, slots=True)
class ConfirmedReceipt:
    """Recibo A1 confirmado. Conta e grupo vêm da configuração."""

    message_ids: tuple[str, ...]
    accepted_at: str
    intent_hash: str


@dataclass(frozen=True, slots=True)
class DeliveryUnknownReceipt:
    reason: DeliveryUnknownReason


# ---------------------------------------------------------------- saídas (DTOs da Edge)


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    outcome: IngestOutcome
    event_id: str
    envelope_id: str
    processing_state: ProcessingState
    identity_state: IdentityState


@dataclass(frozen=True, slots=True)
class ScheduledContact:
    """``current_next_contact`` com ``state: scheduled``; ``state: none`` vira ``None``."""

    job_id: str
    due_at: str


@dataclass(frozen=True, slots=True)
class AppliedReceipt:
    outcome: ApplyOutcome
    document_id: str
    followup_id: str
    event_ids: tuple[str, ...]
    revision: int
    agenda_effect: AgendaEffect
    current_next_contact: ScheduledContact | None
    recorded_at: str
    envelope_id: str
    identity_state: IdentityState


@dataclass(frozen=True, slots=True)
class ClarificationReceipt:
    outcome: ClarificationOutcome
    envelope_id: str
    event_id: str
    processing_state: ProcessingState
    reason: ClarificationReason
    recorded_at: str


@dataclass(frozen=True, slots=True)
class FollowupItem:
    document_id: str
    followup_id: str | None
    revision: int
    operational_status: OperationalStatus
    risk_level: RiskLevel
    owner_display_name: str | None
    identity_state: IdentityState | None
    next_action: str | None
    next_action_due_at: str | None
    current_next_contact: ScheduledContact | None


@dataclass(frozen=True, slots=True)
class FollowupPage:
    items: tuple[FollowupItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class CapturedItem:
    event_id: str
    envelope_id: str
    wa_message_id: str
    actor_jid: str
    message_timestamp: str
    text: str = field(repr=False)
    text_truncated: bool
    quote_message_id: str | None
    quote_actor_jid: str | None


@dataclass(frozen=True, slots=True)
class CapturedPage:
    items: tuple[CapturedItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class Claim:
    job_id: str
    attempt_id: str
    fence: int
    lease_until: str
    followup_id: str
    revision: int
    epoch: int
    owner_jid: str | None


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """``next_window_at`` só vem com ``claims`` vazio (janela D2 fechada)."""

    claims: tuple[Claim, ...]
    next_window_at: str | None


@dataclass(frozen=True, slots=True)
class PreparedIntent:
    attempt_id: str
    fence: int
    lease_until: str
    send_before: str
    intent_hash: str
    group_jid: str
    mention_jid: str | None
    text: str = field(repr=False)
    correlation_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReceiptConfirmation:
    outcome: ReceiptOutcome
    state: ReceiptState
    event_id: str
    message_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Plan(Generic[T]):
    """Pedido já validado: contexto, payload e o parser estrito da resposta desta operação."""

    context: dict[str, object]
    payload: dict[str, object]
    parse: Callable[[object], T]


# ---------------------------------------------------------------- assinatura e JSON estrito


def sign_request(
    *,
    hmac_key: bytes,
    pathname: str,
    key_id: str,
    timestamp: str,
    nonce: str,
    request_id: str,
    body: bytes,
) -> str:
    """HMAC-SHA256 em hex dos bytes que a Edge verifica (§5)."""
    signed = "\n".join([
        SIGNATURE_DOMAIN,
        "POST",
        pathname,
        key_id,
        timestamp,
        nonce,
        request_id,
        hashlib.sha256(body).hexdigest(),
    ])
    return hmac.new(hmac_key, signed.encode("utf-8"), hashlib.sha256).hexdigest()


def _reject_constant(_name: str) -> NoReturn:
    raise ValueError("non_finite_number")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _strict_loads(raw: bytes) -> object:
    """UTF-8 estrito, sem BOM, sem NaN/Infinity e sem chave duplicada."""
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (ValueError, RecursionError):
        _malformed("body:json")


def _log(
    level: int, operation: str, status: int | None, code: str, request_id: str | None
) -> None:
    logger.log(
        level,
        "licencas_http operation=%s status=%s code=%s request_id=%s",
        operation,
        "none" if status is None else status,
        code,
        "none" if request_id is None else request_id,
    )


# ---------------------------------------------------------------- parsers de resposta


def _scheduled_contact(value: object, path: str) -> ScheduledContact | None:
    state = value.get("state") if isinstance(value, dict) else None
    if state == "none":
        _fields(value, path, _malformed, ("state",))
        return None
    if state != "scheduled":
        _malformed(path)
    contact = _fields(value, path, _malformed, ("state", "job_id", "due_at"))
    return ScheduledContact(
        job_id=_text(contact["job_id"], f"{path}.job_id", _malformed, _UUID),
        due_at=_instant(contact["due_at"], f"{path}.due_at", _malformed),
    )


def _followup_item(item: object) -> FollowupItem:
    path = "result.items"
    data = _fields(
        item,
        path,
        _malformed,
        (
            "document_id",
            "followup_id",
            "revision",
            "operational_status",
            "risk_level",
            "owner_display_name",
            "identity_state",
            "next_action",
            "next_action_due_at",
            "current_next_contact",
        ),
    )
    identity = data["identity_state"]
    return FollowupItem(
        document_id=_text(
            data["document_id"], f"{path}.document_id", _malformed, _UUID
        ),
        followup_id=_optional_text(
            data["followup_id"], f"{path}.followup_id", _malformed, _UUID
        ),
        revision=_integer(
            data["revision"], f"{path}.revision", _malformed, 0, MAX_SAFE_INTEGER
        ),
        operational_status=_parse_enum(
            data["operational_status"],
            f"{path}.operational_status",
            _malformed,
            OperationalStatus,
        ),
        risk_level=_parse_enum(
            data["risk_level"], f"{path}.risk_level", _malformed, RiskLevel
        ),
        owner_display_name=_optional_text(
            data["owner_display_name"], f"{path}.owner_display_name", _malformed
        ),
        identity_state=None
        if identity is None
        else _parse_enum(identity, f"{path}.identity_state", _malformed, IdentityState),
        next_action=_optional_text(
            data["next_action"], f"{path}.next_action", _malformed
        ),
        next_action_due_at=_optional_instant(
            data["next_action_due_at"], f"{path}.next_action_due_at", _malformed
        ),
        current_next_contact=_scheduled_contact(
            data["current_next_contact"], f"{path}.current_next_contact"
        ),
    )


def _captured_item(item: object) -> CapturedItem:
    path = "result.items"
    data = _fields(
        item,
        path,
        _malformed,
        (
            "event_id",
            "envelope_id",
            "wa_message_id",
            "actor_jid",
            "message_timestamp",
            "text",
            "text_truncated",
            "quote_message_id",
            "quote_actor_jid",
        ),
    )
    return CapturedItem(
        event_id=_text(data["event_id"], f"{path}.event_id", _malformed, _UUID),
        envelope_id=_text(
            data["envelope_id"], f"{path}.envelope_id", _malformed, _HEX64
        ),
        wa_message_id=_text(
            data["wa_message_id"], f"{path}.wa_message_id", _malformed, _MESSAGE_ID
        ),
        actor_jid=_text(data["actor_jid"], f"{path}.actor_jid", _malformed, _ACTOR_JID),
        message_timestamp=_instant(
            data["message_timestamp"], f"{path}.message_timestamp", _malformed
        ),
        text=_text(data["text"], f"{path}.text", _malformed),
        text_truncated=_flag(
            data["text_truncated"], f"{path}.text_truncated", _malformed
        ),
        quote_message_id=_optional_text(
            data["quote_message_id"],
            f"{path}.quote_message_id",
            _malformed,
            _MESSAGE_ID,
        ),
        quote_actor_jid=_optional_text(
            data["quote_actor_jid"], f"{path}.quote_actor_jid", _malformed, _ACTOR_JID
        ),
    )


def _page(
    data: object, limit: int, parse_item: Callable[[object], T]
) -> tuple[tuple[T, ...], str | None]:
    page = _fields(data, "result", _malformed, ("items", "next_cursor"))
    items = tuple(
        parse_item(item)
        for item in _sequence(page["items"], "result.items", _malformed, 0, limit)
    )
    return items, _optional_text(
        page["next_cursor"], "result.next_cursor", _malformed, _CURSOR
    )


def _cursor(value: object) -> str | None:
    """O cursor é opaco: o cliente só confere a forma HTTP e devolve o que recebeu da Edge."""
    return _optional_text(value, "payload.cursor", _refuse, _CURSOR)


def _attempt_payload(attempt: object) -> dict[str, object]:
    if not isinstance(attempt, AttemptRef):
        _refuse("payload.attempt")
    return {
        "job_id": _text(attempt.job_id, "payload.job_id", _refuse, _UUID),
        "attempt_id": _text(attempt.attempt_id, "payload.attempt_id", _refuse, _UUID),
        "fence": _integer(attempt.fence, "payload.fence", _refuse, 1, MAX_SAFE_INTEGER),
    }


def _next_contact_payload(value: object) -> dict[str, object]:
    path = "payload.patch.next_contact"
    if isinstance(value, NextContactKeep):
        return {"intent": "keep"}
    if isinstance(value, NextContactCancel):
        return {"intent": "cancel"}
    if not isinstance(value, NextContactSchedule):
        _refuse(path)
    return {
        "intent": "schedule",
        "due_at": _instant(value.due_at, f"{path}.due_at", _refuse),
        "timezone": _text(value.timezone, f"{path}.timezone", _refuse, _TIMEZONE),
        "provenance": "owner_agreed",
    }


def _patch_payload(patch: object) -> dict[str, object]:
    if not isinstance(patch, ReplyPatch):
        _refuse("payload.patch")
    result: dict[str, object] = {}
    if patch.operational_status is not UNSET:
        result["operational_status"] = _member(
            patch.operational_status,
            "payload.patch.operational_status",
            _refuse,
            OperationalStatus,
        ).value
    if patch.next_action is not UNSET:
        result["next_action"] = _optional_text(
            patch.next_action, "payload.patch.next_action", _refuse
        )
    if patch.next_action_due_at is not UNSET:
        result["next_action_due_at"] = _optional_instant(
            patch.next_action_due_at, "payload.patch.next_action_due_at", _refuse
        )
    if patch.blocker_summary is not UNSET:
        result["blocker_summary"] = _optional_text(
            patch.blocker_summary, "payload.patch.blocker_summary", _refuse
        )
    if patch.evidence_summary is not UNSET:
        result["evidence_summary"] = _optional_text(
            patch.evidence_summary, "payload.patch.evidence_summary", _refuse
        )
    if patch.next_contact is not UNSET:
        result["next_contact"] = _next_contact_payload(patch.next_contact)
    if not result:
        _refuse("payload.patch")
    return result


def _correlation_payload(correlation: object) -> dict[str, object]:
    path = "payload.correlation"
    if not isinstance(correlation, ReplyCorrelation):
        _refuse(path)
    result: dict[str, object] = {}
    if correlation.quote_message_id is not None:
        result["quote_message_id"] = _text(
            correlation.quote_message_id, path, _refuse, _MESSAGE_ID
        )
    if correlation.token is not None:
        result["token"] = _text(correlation.token, path, _refuse, _CORRELATION_TOKEN)
    if not result:
        _refuse(path)
    return result


# ---------------------------------------------------------------- cliente


class LicencasClient:
    """Cliente assíncrono da fachada. Um pedido HTTP por chamada de método."""

    def __init__(
        self,
        config: LicencasClientConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        loopback_test_port: int | None = None,
    ) -> None:
        """``loopback_test_port`` troca o destino por HTTP em 127.0.0.1 nessa porta.

        Existe só para a prova de interoperabilidade local. Não há variável de ambiente nem
        campo de configuração que chegue a ele: ``from_env`` não o aceita.
        """
        if not isinstance(config, LicencasClientConfig):
            raise LicencasConfigError("config")
        self._config = config
        self._clock = clock
        if loopback_test_port is None:
            self._url = config.endpoint
        elif type(loopback_test_port) is int and 1 <= loopback_test_port <= 65535:
            self._url = f"http://{_LOOPBACK_TEST_HOST}:{loopback_test_port}{config.signed_pathname}"
        else:
            raise LicencasConfigError("loopback_test_port")
        self._http = httpx.AsyncClient(
            transport=transport,
            verify=True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_S,
                read=READ_TIMEOUT_S,
                write=WRITE_TIMEOUT_S,
                pool=POOL_TIMEOUT_S,
            ),
            # Nenhum cookie é guardado nem reenviado.
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
        )

    @classmethod
    def from_env(
        cls, read: Callable[[str], str | None] = os.environ.get
    ) -> LicencasClient:
        """Caminho de produção: configuração completa do ambiente ou nenhum cliente."""
        return cls(load_licencas_config(read))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> LicencasClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------ contextos

    def _scope(self) -> dict[str, object]:
        config = self._config
        return {
            "principal_id": config.principal_id,
            "instance_id": config.instance_id,
            "policy_id": config.policy_id,
            "group_jid": config.group_jid,
            "policy_epoch": config.policy_epoch,
            "account_binding_hash": config.account_binding_hash,
        }

    def _context(self, operation: str, source: object) -> dict[str, object]:
        """O mapa literal ``OPERATIONS`` decide o tipo de contexto de cada operação."""
        if OPERATIONS[operation] == "member":
            return self._actor_context(source)
        return self._worker_context(source)

    def _actor_context(self, actor: object) -> dict[str, object]:
        if not isinstance(actor, ActorContext) or not isinstance(
            actor.membership, MembershipAttestation
        ):
            _refuse("context")
        context = {
            **self._scope(),
            "actor_jid": _text(
                actor.actor_jid, "context.actor_jid", _refuse, _ACTOR_JID
            ),
            "actor_display_name": _display_name(
                actor.actor_display_name, "context.actor_display_name"
            ),
            "wa_message_id": _text(
                actor.wa_message_id, "context.wa_message_id", _refuse, _MESSAGE_ID
            ),
            "envelope_id": _text(
                actor.envelope_id, "context.envelope_id", _refuse, _HEX64
            ),
            "message_timestamp": _instant(
                actor.message_timestamp, "context.message_timestamp", _refuse
            ),
            "observed_at": _instant(actor.observed_at, "context.observed_at", _refuse),
            "member_checked_at": _instant(
                actor.membership.checked_at, "context.member_checked_at", _refuse
            ),
            "actor_is_current_member": _flag(
                actor.membership.is_current_member,
                "context.actor_is_current_member",
                _refuse,
            ),
            "phone_e164": None,
            "phone_attestation_source": None,
            "phone_attested_at": None,
        }
        if actor.phone is not None:
            if not isinstance(actor.phone, PhoneAttestation):
                _refuse("context.phone_e164")
            context["phone_e164"] = _text(
                actor.phone.e164, "context.phone_e164", _refuse, _E164
            )
            context["phone_attestation_source"] = _text(
                actor.phone.source,
                "context.phone_attestation_source",
                _refuse,
                _ATTESTATION_SOURCE,
            )
            context["phone_attested_at"] = _instant(
                actor.phone.attested_at, "context.phone_attested_at", _refuse
            )
        # Sem atestado positivo do transporte não há pedido: a negativa é local, zero RPC.
        if context["actor_is_current_member"] is not True:
            raise _Refusal("membership_not_attested", "context.actor_is_current_member")
        return context

    def _worker_context(self, worker: object) -> dict[str, object]:
        if not isinstance(worker, WorkerContext):
            _refuse("context")
        observed_at = _instant(worker.observed_at, "context.observed_at", _refuse)
        return {
            **self._scope(),
            "actor_jid": None,
            "actor_display_name": None,
            "wa_message_id": None,
            "envelope_id": None,
            # Worker não tem mensagem: o instante exigido pelo contexto é o da própria observação.
            "message_timestamp": observed_at,
            "observed_at": observed_at,
            "member_checked_at": None,
            "actor_is_current_member": False,
            "phone_e164": None,
            "phone_attestation_source": None,
            "phone_attested_at": None,
        }

    # ------------------------------------------------------------ operações

    async def ingest_envelope(
        self, actor: ActorContext, envelope: InboundEnvelope
    ) -> IngestReceipt:
        """``ingestEnvelope``: captura bruta do envelope A1 (ack do A1 só depois deste recibo)."""

        def plan() -> _Plan[IngestReceipt]:
            context = self._context("ingestEnvelope", actor)
            if not isinstance(envelope, InboundEnvelope):
                _refuse("payload.envelope")
            path = "payload.envelope"
            attachment = envelope.attachment_metadata
            if attachment is not None and not isinstance(
                attachment, AttachmentMetadata
            ):
                _refuse(f"{path}.attachment_metadata")
            payload_envelope: dict[str, object] = {
                "envelope_id": context["envelope_id"],
                "instance_id": self._config.instance_id,
                "group_jid": self._config.group_jid,
                "actor_jid": context["actor_jid"],
                "wa_message_id": context["wa_message_id"],
                "message_timestamp": context["message_timestamp"],
                "received_at": _instant(
                    envelope.received_at, f"{path}.received_at", _refuse
                ),
                "text": _text(envelope.text, f"{path}.text", _refuse),
                "text_truncated": _flag(
                    envelope.text_truncated, f"{path}.text_truncated", _refuse
                ),
                "quote_message_id": _optional_text(
                    envelope.quote_message_id,
                    f"{path}.quote_message_id",
                    _refuse,
                    _MESSAGE_ID,
                ),
                "quote_actor_jid": _optional_text(
                    envelope.quote_actor_jid,
                    f"{path}.quote_actor_jid",
                    _refuse,
                    _ACTOR_JID,
                ),
                "attachment_metadata": None
                if attachment is None
                else {
                    "media_type": _text(
                        attachment.media_type,
                        f"{path}.attachment_metadata",
                        _refuse,
                        _MEDIA_TYPE,
                    ),
                    "size_bytes": _integer(
                        attachment.size_bytes,
                        f"{path}.attachment_metadata",
                        _refuse,
                        0,
                        MAX_SAFE_INTEGER,
                    ),
                },
            }
            envelope_id = actor.envelope_id

            def parse(data: object) -> IngestReceipt:
                result = _fields(
                    data,
                    "result",
                    _malformed,
                    (
                        "outcome",
                        "event_id",
                        "envelope_id",
                        "processing_state",
                        "identity_state",
                    ),
                )
                if result["envelope_id"] != envelope_id:
                    _malformed("result.envelope_id")
                return IngestReceipt(
                    outcome=_parse_enum(
                        result["outcome"], "result.outcome", _malformed, IngestOutcome
                    ),
                    event_id=_text(
                        result["event_id"], "result.event_id", _malformed, _UUID
                    ),
                    envelope_id=envelope_id,
                    processing_state=_parse_enum(
                        result["processing_state"],
                        "result.processing_state",
                        _malformed,
                        ProcessingState,
                    ),
                    identity_state=_parse_enum(
                        result["identity_state"],
                        "result.identity_state",
                        _malformed,
                        IdentityState,
                    ),
                )

            return _Plan(context, {"envelope": payload_envelope}, parse)

        return await self._execute("ingestEnvelope", plan)

    async def record_reply(
        self, actor: ActorContext, disposition: ApplyReply | NeedsClarification
    ) -> AppliedReceipt | ClarificationReceipt:
        """``recordReply``: disposição explícita (§1.4). O recibo devolvido é sempre o do ramo pedido."""

        def plan() -> _Plan[AppliedReceipt | ClarificationReceipt]:
            context = self._context("recordReply", actor)
            envelope_id = actor.envelope_id
            if isinstance(disposition, ApplyReply):
                payload: dict[str, object] = {
                    "envelope_id": envelope_id,
                    "disposition": {"kind": "apply"},
                    "correlation": _correlation_payload(disposition.correlation),
                    "patch": _patch_payload(disposition.patch),
                }
                return _Plan(
                    context, payload, lambda data: _applied_receipt(data, envelope_id)
                )
            if not isinstance(disposition, NeedsClarification):
                _refuse("payload.disposition")
            reason = _member(
                disposition.reason,
                "payload.disposition.reason",
                _refuse,
                ClarificationReason,
            )
            payload = {
                "envelope_id": envelope_id,
                "disposition": {"kind": "needs_clarification", "reason": reason.value},
            }
            return _Plan(
                context,
                payload,
                lambda data: _clarification_receipt(data, envelope_id, reason),
            )

        return await self._execute("recordReply", plan)

    async def query_followups(
        self,
        actor: ActorContext,
        filter: FollowupFilter,
        *,
        limit: int,
        cursor: str | None,
    ) -> FollowupPage:
        """``queryFollowups``: página de acompanhamentos (limite 1..50; cursor ``None`` = primeira página)."""

        def plan() -> _Plan[FollowupPage]:
            context = self._context("queryFollowups", actor)
            if not isinstance(filter, FollowupFilter):
                _refuse("payload.filter")
            selected: dict[str, object] = {}
            if filter.document_id is not None:
                selected["document_id"] = _text(
                    filter.document_id, "payload.filter", _refuse, _UUID
                )
            if filter.operational_status is not None:
                selected["operational_status"] = _member(
                    filter.operational_status,
                    "payload.filter",
                    _refuse,
                    OperationalStatus,
                ).value
            if filter.risk_level is not None:
                selected["risk_level"] = _member(
                    filter.risk_level, "payload.filter", _refuse, RiskLevel
                ).value
            size = _integer(limit, "payload.limit", _refuse, 1, 50)
            payload: dict[str, object] = {
                "filter": selected,
                "limit": size,
                "cursor": _cursor(cursor),
            }
            return _Plan(
                context,
                payload,
                lambda data: FollowupPage(*_page(data, size, _followup_item)),
            )

        return await self._execute("queryFollowups", plan)

    async def list_captured(
        self, worker: WorkerContext, *, limit: int, cursor: str | None
    ) -> CapturedPage:
        """``listCaptured``: capturas ainda em ``captured`` (retomada do consumidor)."""

        def plan() -> _Plan[CapturedPage]:
            context = self._context("listCaptured", worker)
            size = _integer(limit, "payload.limit", _refuse, 1, 50)
            payload: dict[str, object] = {"limit": size, "cursor": _cursor(cursor)}
            return _Plan(
                context,
                payload,
                lambda data: CapturedPage(*_page(data, size, _captured_item)),
            )

        return await self._execute("listCaptured", plan)

    async def claim_due(
        self, worker: WorkerContext, *, limit: int, lease_seconds: int
    ) -> ClaimResult:
        """``claimDue``: reivindica cobranças vencidas (limite 1..20, lease 10..120 s)."""

        def plan() -> _Plan[ClaimResult]:
            context = self._context("claimDue", worker)
            size = _integer(limit, "payload.limit", _refuse, 1, 20)
            lease = _integer(lease_seconds, "payload.lease_seconds", _refuse, 10, 120)
            epoch = self._config.policy_epoch
            payload: dict[str, object] = {"limit": size, "lease_seconds": lease}
            return _Plan(
                context, payload, lambda data: _claim_result(data, size, epoch)
            )

        return await self._execute("claimDue", plan)

    async def prepare_intent(
        self,
        worker: WorkerContext,
        attempt: AttemptRef,
        owner_membership: OwnerMembership | None,
    ) -> PreparedIntent:
        """``prepareIntent``: grava o intent e devolve o texto exato a enviar. ``None`` = cobrança sem nome."""

        def plan() -> _Plan[PreparedIntent]:
            context = self._context("prepareIntent", worker)
            payload = _attempt_payload(attempt)
            path = "payload.owner_membership"
            if owner_membership is None:
                payload["owner_membership"] = None
            elif isinstance(owner_membership, OwnerMembership):
                payload["owner_membership"] = {
                    "owner_jid": _text(
                        owner_membership.owner_jid,
                        f"{path}.owner_jid",
                        _refuse,
                        _OWNER_JID,
                    ),
                    "is_current_member": _flag(
                        owner_membership.is_current_member,
                        f"{path}.is_current_member",
                        _refuse,
                    ),
                    "checked_at": _instant(
                        owner_membership.checked_at,
                        f"{path}.checked_at",
                        _refuse,
                        _SECONDS_INSTANT,
                    ),
                }
            else:
                _refuse(path)
            group_jid = self._config.group_jid
            return _Plan(
                context,
                payload,
                lambda data: _prepared_intent(data, attempt, group_jid),
            )

        return await self._execute("prepareIntent", plan)

    async def confirm_receipt(
        self,
        worker: WorkerContext,
        attempt: AttemptRef,
        receipt: ConfirmedReceipt | DeliveryUnknownReceipt,
    ) -> ReceiptConfirmation:
        """``confirmReceipt``: recibo confirmado do A1 ou incerteza de entrega da MESMA tentativa."""

        def plan() -> _Plan[ReceiptConfirmation]:
            context = self._context("confirmReceipt", worker)
            payload = _attempt_payload(attempt)
            path = "payload.receipt"
            if isinstance(receipt, DeliveryUnknownReceipt):
                reason = _member(
                    receipt.reason, f"{path}.reason", _refuse, DeliveryUnknownReason
                )
                payload["receipt"] = {
                    "state": "delivery_unknown",
                    "reason": reason.value,
                }
                return _Plan(
                    context, payload, lambda data: _receipt_confirmation(data, None)
                )
            if not isinstance(receipt, ConfirmedReceipt) or not isinstance(
                receipt.message_ids, tuple
            ):
                _refuse(path)
            message_ids = _unique_ids(
                list(receipt.message_ids),
                f"{path}.message_ids",
                _refuse,
                _RECEIPT_MESSAGE_ID,
                1,
                16,
            )
            payload["receipt"] = {
                "state": "confirmed",
                "message_ids": list(message_ids),
                "accepted_at": _instant(
                    receipt.accepted_at,
                    f"{path}.accepted_at",
                    _refuse,
                    _SECONDS_INSTANT,
                ),
                "account_binding_hash": self._config.account_binding_hash,
                "group_jid": self._config.group_jid,
                "intent_hash": _text(
                    receipt.intent_hash, f"{path}.intent_hash", _refuse, _HEX64
                ),
            }
            return _Plan(
                context, payload, lambda data: _receipt_confirmation(data, message_ids)
            )

        return await self._execute("confirmReceipt", plan)

    # ------------------------------------------------------------ pedido único

    async def _execute(self, operation: str, plan: Callable[[], _Plan[T]]) -> T:
        try:
            planned = plan()
            body = _encode_body(operation, planned.context, planned.payload)
        except _Refusal as refusal:
            _log(logging.WARNING, operation, None, refusal.code, None)
            raise LicencasRequestError(
                operation=operation,
                request_id=None,
                status=None,
                code=refusal.code,
                detail=refusal.path,
            ) from None
        request_id = str(uuid.uuid4())
        status, raw = await self._post(operation, request_id, body)
        try:
            if status != 200:
                raise self._http_failure(operation, request_id, status, raw)
            result = planned.parse(_strict_loads(raw))
        except _Malformed as malformed:
            _log(logging.WARNING, operation, status, "invalid_response", request_id)
            raise LicencasResponseError(
                operation=operation,
                request_id=request_id,
                status=status,
                code="invalid_response",
                detail=malformed.path,
            ) from None
        _log(logging.INFO, operation, status, "ok", request_id)
        return result

    async def _post(
        self, operation: str, request_id: str, body: bytes
    ) -> tuple[int, bytes]:
        """Envia exatamente os bytes assinados, uma única vez, e lê a resposta com limite de tamanho."""
        config = self._config
        timestamp = str(int(self._clock()))
        nonce = secrets.token_hex(NONCE_BYTES)
        headers = {
            "Content-Type": "application/json",
            "X-Licencas-Capability": config.capability,
            "X-Licencas-Key-Id": config.key_id,
            "X-Licencas-Timestamp": timestamp,
            "X-Licencas-Nonce": nonce,
            "X-Licencas-Request-Id": request_id,
            "X-Licencas-Signature": sign_request(
                hmac_key=config.hmac_key,
                pathname=config.signed_pathname,
                key_id=config.key_id,
                timestamp=timestamp,
                nonce=nonce,
                request_id=request_id,
                body=body,
            ),
        }
        received = bytearray()
        try:
            async with self._http.stream(
                "POST", self._url, content=body, headers=headers
            ) as response:
                status = response.status_code
                async for chunk in response.aiter_bytes():
                    received += chunk
                    if len(received) > MAX_RESPONSE_BYTES:
                        break
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise self._failed(
                LicencasNotSentError, operation, request_id, None, "connect_failed"
            ) from None
        except httpx.TimeoutException:
            raise self._failed(
                LicencasOutcomeUnknownError, operation, request_id, None, "timeout"
            ) from None
        except httpx.HTTPError:
            raise self._failed(
                LicencasOutcomeUnknownError,
                operation,
                request_id,
                None,
                "network_error",
            ) from None
        if len(received) > MAX_RESPONSE_BYTES:
            raise self._failed(
                LicencasResponseError,
                operation,
                request_id,
                status,
                "response_too_large",
            )
        return status, bytes(received)

    @staticmethod
    def _failed(
        kind: type[LicencasError],
        operation: str,
        request_id: str,
        status: int | None,
        code: str,
    ) -> LicencasError:
        _log(logging.WARNING, operation, status, code, request_id)
        return kind(
            operation=operation, request_id=request_id, status=status, code=code
        )

    def _http_failure(
        self, operation: str, request_id: str, status: int, raw: bytes
    ) -> LicencasError:
        """Status diferente de 200 -> erro tipado. Corpo fora de ``{"error": código}`` levanta ``_Malformed``."""
        if 300 <= status < 400:
            return self._failed(
                LicencasResponseError, operation, request_id, status, "redirect_refused"
            )
        if status not in _ERROR_STATUSES:
            return self._failed(
                LicencasResponseError,
                operation,
                request_id,
                status,
                "unexpected_status",
            )
        error = _fields(_strict_loads(raw), "error", _malformed, ("error",))
        code = _text(error["error"], "error.code", _malformed, _ERROR_CODE)
        kind: type[LicencasError] = LicencasRejectedError
        if status == 503:
            kind = LicencasBusyError if code == "busy" else LicencasUnavailableError
        elif status == 429:
            kind = LicencasRateLimitedError
        elif status == 409 and code == "replay_unconfirmed":
            kind = LicencasReplayUnconfirmedError
        return self._failed(kind, operation, request_id, status, code)


def _encode_body(
    operation: str, context: dict[str, object], payload: dict[str, object]
) -> bytes:
    """JSON estrito (sem NaN/Infinity), UTF-8, no máximo 16 KiB; estes são os bytes assinados e enviados."""
    document = {
        "version": 1,
        "operation": operation,
        "context": context,
        "payload": payload,
    }
    try:
        body = json.dumps(
            document, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError):
        raise _Refusal("invalid_json", "body") from None
    if len(body) > MAX_BODY_BYTES:
        raise _Refusal("payload_too_large", "body")
    return body


def _applied_receipt(data: object, envelope_id: str) -> AppliedReceipt:
    result = _fields(
        data,
        "result",
        _malformed,
        (
            "outcome",
            "document_id",
            "followup_id",
            "event_ids",
            "revision",
            "agenda_effect",
            "current_next_contact",
            "recorded_at",
            "envelope_id",
            "identity_state",
        ),
    )
    if result["envelope_id"] != envelope_id:
        _malformed("result.envelope_id")
    return AppliedReceipt(
        outcome=_parse_enum(
            result["outcome"], "result.outcome", _malformed, ApplyOutcome
        ),
        document_id=_text(
            result["document_id"], "result.document_id", _malformed, _UUID
        ),
        followup_id=_text(
            result["followup_id"], "result.followup_id", _malformed, _UUID
        ),
        event_ids=_unique_ids(
            result["event_ids"], "result.event_ids", _malformed, _UUID, 1, 20
        ),
        revision=_integer(
            result["revision"], "result.revision", _malformed, 1, MAX_SAFE_INTEGER
        ),
        agenda_effect=_parse_enum(
            result["agenda_effect"], "result.agenda_effect", _malformed, AgendaEffect
        ),
        current_next_contact=_scheduled_contact(
            result["current_next_contact"], "result.current_next_contact"
        ),
        recorded_at=_instant(result["recorded_at"], "result.recorded_at", _malformed),
        envelope_id=envelope_id,
        identity_state=_parse_enum(
            result["identity_state"], "result.identity_state", _malformed, IdentityState
        ),
    )


def _clarification_receipt(
    data: object, envelope_id: str, reason: ClarificationReason
) -> ClarificationReceipt:
    result = _fields(
        data,
        "result",
        _malformed,
        (
            "outcome",
            "envelope_id",
            "event_id",
            "processing_state",
            "reason",
            "recorded_at",
        ),
    )
    if result["envelope_id"] != envelope_id:
        _malformed("result.envelope_id")
    if result["reason"] != reason.value:
        _malformed("result.reason")
    return ClarificationReceipt(
        outcome=_parse_enum(
            result["outcome"], "result.outcome", _malformed, ClarificationOutcome
        ),
        envelope_id=envelope_id,
        event_id=_text(result["event_id"], "result.event_id", _malformed, _UUID),
        processing_state=_parse_enum(
            result["processing_state"],
            "result.processing_state",
            _malformed,
            ProcessingState,
            (ProcessingState.NEEDS_CLARIFICATION,),
        ),
        reason=reason,
        recorded_at=_instant(result["recorded_at"], "result.recorded_at", _malformed),
    )


def _claim(item: object, epoch: int) -> Claim:
    path = "result.claims"
    data = _fields(
        item,
        path,
        _malformed,
        (
            "job_id",
            "attempt_id",
            "fence",
            "lease_until",
            "followup_id",
            "revision",
            "epoch",
            "owner_jid",
        ),
    )
    # O epoch reivindicado é o do contexto autorizado: outro valor não é desta configuração.
    if type(data["epoch"]) is not int or data["epoch"] != epoch:
        _malformed(f"{path}.epoch")
    return Claim(
        job_id=_text(data["job_id"], f"{path}.job_id", _malformed, _UUID),
        attempt_id=_text(data["attempt_id"], f"{path}.attempt_id", _malformed, _UUID),
        fence=_integer(data["fence"], f"{path}.fence", _malformed, 1, MAX_SAFE_INTEGER),
        lease_until=_instant(
            data["lease_until"], f"{path}.lease_until", _malformed, _UTC_MICROS
        ),
        followup_id=_text(
            data["followup_id"], f"{path}.followup_id", _malformed, _UUID
        ),
        revision=_integer(
            data["revision"], f"{path}.revision", _malformed, 0, MAX_SAFE_INTEGER
        ),
        epoch=epoch,
        owner_jid=_optional_text(
            data["owner_jid"], f"{path}.owner_jid", _malformed, _OWNER_JID
        ),
    )


def _claim_result(data: object, limit: int, epoch: int) -> ClaimResult:
    result = _fields(data, "result", _malformed, ("claims",), ("next_window_at",))
    claims = tuple(
        _claim(item, epoch)
        for item in _sequence(result["claims"], "result.claims", _malformed, 0, limit)
    )
    if len({claim.job_id for claim in claims}) != len(claims) or len({
        claim.attempt_id for claim in claims
    }) != len(claims):
        _malformed("result.claims:duplicate")
    if "next_window_at" not in result:
        return ClaimResult(claims=claims, next_window_at=None)
    if claims:
        _malformed("result.next_window_at")
    return ClaimResult(
        claims=(),
        next_window_at=_instant(
            result["next_window_at"], "result.next_window_at", _malformed, _UTC_MICROS
        ),
    )


def _prepared_intent(
    data: object, attempt: AttemptRef, group_jid: str
) -> PreparedIntent:
    result = _fields(
        data,
        "result",
        _malformed,
        (
            "attempt_id",
            "fence",
            "lease_until",
            "send_before",
            "intent_hash",
            "group_jid",
            "mention_jid",
            "text",
            "correlation_token",
        ),
    )
    if result["attempt_id"] != attempt.attempt_id:
        _malformed("result.attempt_id")
    if type(result["fence"]) is not int or result["fence"] != attempt.fence:
        _malformed("result.fence")
    if result["group_jid"] != group_jid:
        _malformed("result.group_jid")
    lease_until = _instant(
        result["lease_until"], "result.lease_until", _malformed, _UTC_MICROS
    )
    send_before = _instant(
        result["send_before"], "result.send_before", _malformed, _UTC_MICROS
    )
    # Os dois são UTC de largura fixa: a ordem do texto é a ordem do tempo. Nunca depois da lease.
    if send_before > lease_until:
        _malformed("result.send_before")
    text = _text(result["text"], "result.text", _malformed)
    if text == "":
        _malformed("result.text")
    return PreparedIntent(
        attempt_id=attempt.attempt_id,
        fence=attempt.fence,
        lease_until=lease_until,
        send_before=send_before,
        intent_hash=_text(
            result["intent_hash"], "result.intent_hash", _malformed, _HEX64
        ),
        group_jid=group_jid,
        mention_jid=_optional_text(
            result["mention_jid"], "result.mention_jid", _malformed, _OWNER_JID
        ),
        text=text,
        correlation_token=_text(
            result["correlation_token"],
            "result.correlation_token",
            _malformed,
            _PROMPT_TOKEN,
        ),
    )


def _receipt_confirmation(
    data: object, message_ids: tuple[str, ...] | None
) -> ReceiptConfirmation:
    """``message_ids`` = IDs enviados no recibo confirmado, ou ``None`` no recibo de incerteza."""
    result = _fields(
        data, "result", _malformed, ("outcome", "state", "event_id", "message_ids")
    )
    event_id = _text(result["event_id"], "result.event_id", _malformed, _UUID)
    if message_ids is None:
        _sequence(result["message_ids"], "result.message_ids", _malformed, 0, 0)
        return ReceiptConfirmation(
            outcome=_parse_enum(
                result["outcome"],
                "result.outcome",
                _malformed,
                ReceiptOutcome,
                (ReceiptOutcome.RECORDED_UNKNOWN,),
            ),
            state=_parse_enum(
                result["state"],
                "result.state",
                _malformed,
                ReceiptState,
                (ReceiptState.DELIVERY_UNKNOWN,),
            ),
            event_id=event_id,
            message_ids=(),
        )
    # Recibo confirmado: os IDs devolvidos são exatamente os enviados, na mesma ordem.
    if result["message_ids"] != list(message_ids):
        _malformed("result.message_ids")
    return ReceiptConfirmation(
        outcome=_parse_enum(
            result["outcome"],
            "result.outcome",
            _malformed,
            ReceiptOutcome,
            (ReceiptOutcome.CONFIRMED, ReceiptOutcome.ALREADY_CONFIRMED),
        ),
        state=_parse_enum(
            result["state"],
            "result.state",
            _malformed,
            ReceiptState,
            (ReceiptState.CONFIRMED,),
        ),
        event_id=event_id,
        message_ids=message_ids,
    )
