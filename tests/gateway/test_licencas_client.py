"""Fronteira de UNIDADE do cliente HTTP tipado de Licenças (A2-client).

Todo teste deste arquivo usa ``httpx.MockTransport``: nenhum socket é aberto e a
Edge real NÃO participa. Isto prova o que o cliente envia e como ele trata cada
resposta; não prova integração. A interoperabilidade com o handler real da Edge
fica fora do repositório (``licencas-hermes-evidence/a2-client/interop``).
Todos os valores abaixo são sintéticos.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import httpx
import pytest

import gateway.licencas_client as lc

aio = pytest.mark.asyncio

NOW = 1_800_000_000
HMAC_KEY = bytes(range(32))
CAPABILITY = "synthetic-capability-" + "0" * 32
POLICY_ID = "0a0a0a0a-1111-4222-8333-444444444444"
GROUP_JID = "120363000000000001@g.us"
INSTANCE_ID = "5511999990000@s.whatsapp.net"
BINDING_HASH = "ab" * 32
ENV = {
    "LICENCAS_API_URL": "https://licencas.invalid/functions/v1/licencas-api",
    "LICENCAS_API_SIGNED_PATHNAME": "/licencas-api",
    "LICENCAS_CAPABILITY": CAPABILITY,
    "LICENCAS_HMAC_KEY_B64": base64.b64encode(HMAC_KEY).decode("ascii"),
    "LICENCAS_KEY_ID": "k1",
    "LICENCAS_PRINCIPAL_ID": "hermes-gateway",
    "LICENCAS_INSTANCE_ID": INSTANCE_ID,
    "LICENCAS_POLICY_ID": POLICY_ID,
    "LICENCAS_GROUP_JID": GROUP_JID,
    "LICENCAS_POLICY_EPOCH": "3",
    "LICENCAS_ACCOUNT_BINDING_HASH": BINDING_HASH,
}

SECRET_NAMES = ("LICENCAS_CAPABILITY", "LICENCAS_HMAC_KEY_B64")
SECRETS = {name: ENV[name] for name in SECRET_NAMES}
YAML_SECTION: dict[str, object] = {
    "enabled": False,
    "api_url": ENV["LICENCAS_API_URL"],
    "signed_pathname": ENV["LICENCAS_API_SIGNED_PATHNAME"],
    "key_id": ENV["LICENCAS_KEY_ID"],
    "principal_id": ENV["LICENCAS_PRINCIPAL_ID"],
    "instance_id": INSTANCE_ID,
    "policy_id": POLICY_ID,
    "group_jid": GROUP_JID,
    "policy_epoch": 3,
    "account_binding_hash": BINDING_HASH,
}
YAML_REQUIRED = tuple(name for name in YAML_SECTION if name != "enabled")


#: Raiz bruta sintética do config.yaml: as fixtures não dependem do disco.
RAW_CONFIG: dict[str, object] = {"licencas_followup": dict(YAML_SECTION)}


def load_config(
    read: Callable[[str], str | None] = SECRETS.get,
) -> lc.LicencasClientConfig:
    return lc.load_licencas_config(read, raw_config=RAW_CONFIG)


ACTOR_JID = "5511988887777@s.whatsapp.net"
OWNER_JID = "5511977776666@s.whatsapp.net"
PHONE = "+5511988887777"
WA_ID = "3EB0ABCDEF1234567890"
QUOTE_ID = "3EB0QUOTED0000000001"
ENVELOPE_ID = "c1" * 32
INTENT_HASH = "d4" * 32
PROMPT_TOKEN = "ab12" * 10
REPLY_TOKEN = "tok_synthetic_reply_token_0001"
TEXT = "  Resposta sintética: protocolo entregue ✅  "
PROMPT_TEXT = "Acompanhamento de licença (texto sintético)\nCódigo: " + PROMPT_TOKEN
DOC_ID = "11111111-1111-4111-8111-111111111111"
FOLLOWUP_ID = "22222222-2222-4222-8222-222222222222"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
JOB_ID = "44444444-4444-4444-8444-444444444444"
ATTEMPT_ID = "55555555-5555-4555-8555-555555555555"
MESSAGE_IDS = ("3EB0SENT00000001", "3EB0SENT00000002")
CURSOR = "c1.kid1.cGF5bG9hZA.bWFj"
NEXT_CURSOR = "c1.kid1.bmV4dA.bWFj"
T_MSG = "2026-11-27T12:00:00Z"
T_CHECKED = "2026-11-27T12:00:04Z"
T_OBSERVED = "2026-11-27T12:00:05Z"
T_RECEIVED = "2026-11-27T12:00:01.250Z"
T_UTC = "2026-11-27T12:00:06.000000Z"
T_LEASE = "2026-11-27T12:01:06.000000Z"
T_SEND_BEFORE = "2026-11-27T12:00:11.000000Z"

SCOPE = {
    "principal_id": "hermes-gateway",
    "instance_id": INSTANCE_ID,
    "policy_id": POLICY_ID,
    "group_jid": GROUP_JID,
    "policy_epoch": 3,
    "account_binding_hash": BINDING_HASH,
}
ACTOR_CONTEXT = {
    **SCOPE,
    "actor_jid": ACTOR_JID,
    "actor_display_name": "Participante Sintético",
    "wa_message_id": WA_ID,
    "envelope_id": ENVELOPE_ID,
    "message_timestamp": T_MSG,
    "observed_at": T_OBSERVED,
    "member_checked_at": T_CHECKED,
    "actor_is_current_member": True,
    "phone_e164": None,
    "phone_attestation_source": None,
    "phone_attested_at": None,
}
WORKER_CONTEXT = {
    **SCOPE,
    "actor_jid": None,
    "actor_display_name": None,
    "wa_message_id": None,
    "envelope_id": None,
    "message_timestamp": T_OBSERVED,
    "observed_at": T_OBSERVED,
    "member_checked_at": None,
    "actor_is_current_member": False,
    "phone_e164": None,
    "phone_attestation_source": None,
    "phone_attested_at": None,
}
SENSITIVE = (
    CAPABILITY,
    ENV["LICENCAS_HMAC_KEY_B64"],
    ACTOR_JID,
    OWNER_JID,
    GROUP_JID,
    INSTANCE_ID,
    PHONE,
    WA_ID,
    QUOTE_ID,
    ENVELOPE_ID,
    INTENT_HASH,
    BINDING_HASH,
    PROMPT_TOKEN,
    REPLY_TOKEN,
    "protocolo entregue",
    "texto sintético",
    MESSAGE_IDS[0],
    CURSOR,
    NEXT_CURSOR,
)
ENDPOINT_HOST = "licencas.invalid"


def actor(**overrides: object) -> lc.ActorContext:
    base = lc.ActorContext(
        actor_jid=ACTOR_JID,
        actor_display_name="Participante Sintético",
        wa_message_id=WA_ID,
        envelope_id=ENVELOPE_ID,
        message_timestamp=T_MSG,
        observed_at=T_OBSERVED,
        membership=lc.MembershipAttestation(
            is_current_member=True, checked_at=T_CHECKED
        ),
        phone=None,
    )
    return dataclasses.replace(base, **overrides)


WORKER = lc.WorkerContext(observed_at=T_OBSERVED)
ATTEMPT = lc.AttemptRef(job_id=JOB_ID, attempt_id=ATTEMPT_ID, fence=7)
ENVELOPE = lc.InboundEnvelope(
    received_at=T_RECEIVED,
    text=TEXT,
    text_truncated=False,
    quote_message_id=QUOTE_ID,
    quote_actor_jid=OWNER_JID,
    attachment_metadata=lc.AttachmentMetadata(
        media_type="application/pdf", size_bytes=1024
    ),
)
OWNER_MEMBERSHIP = lc.OwnerMembership(
    owner_jid=OWNER_JID, is_current_member=True, checked_at=T_CHECKED
)
APPLY = lc.ApplyReply(
    correlation=lc.ReplyCorrelation(quote_message_id=QUOTE_ID, token=REPLY_TOKEN),
    patch=lc.ReplyPatch(
        operational_status=lc.OperationalStatus.IN_PROGRESS,
        next_action=None,
        next_contact=lc.NextContactSchedule(
            due_at="2026-12-01T09:00:00-03:00", timezone="America/Sao_Paulo"
        ),
    ),
)
CONFIRMED = lc.ConfirmedReceipt(
    message_ids=MESSAGE_IDS, accepted_at=T_OBSERVED, intent_hash=INTENT_HASH
)

Call = Callable[[lc.LicencasClient], Awaitable[object]]


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    operation: str
    context: dict[str, object]
    call: Call
    payload: dict[str, object]
    reply: dict[str, object]
    result: object


QUERY_ITEM = {
    "document_id": DOC_ID,
    "followup_id": FOLLOWUP_ID,
    "revision": 4,
    "operational_status": "in_progress",
    "risk_level": "high",
    "owner_display_name": "Participante Sintético",
    "identity_state": "transport_attested",
    "next_action": "Protocolar renovação",
    "next_action_due_at": "2026-12-01T12:00:00Z",
    "current_next_contact": {"state": "scheduled", "job_id": JOB_ID, "due_at": T_UTC},
}
CAPTURED_ITEM = {
    "event_id": EVENT_ID,
    "envelope_id": ENVELOPE_ID,
    "wa_message_id": WA_ID,
    "actor_jid": ACTOR_JID,
    "message_timestamp": T_MSG,
    "text": TEXT,
    "text_truncated": False,
    "quote_message_id": None,
    "quote_actor_jid": None,
}
CLAIM = {
    "job_id": JOB_ID,
    "attempt_id": ATTEMPT_ID,
    "fence": 7,
    "lease_until": T_LEASE,
    "followup_id": FOLLOWUP_ID,
    "revision": 4,
    "epoch": 3,
    "owner_jid": OWNER_JID,
}
ATTEMPT_PAYLOAD = {"job_id": JOB_ID, "attempt_id": ATTEMPT_ID, "fence": 7}

SCENARIOS = [
    Scenario(
        name="ingestEnvelope",
        operation="ingestEnvelope",
        context=ACTOR_CONTEXT,
        call=lambda c: c.ingest_envelope(actor(), ENVELOPE),
        payload={
            "envelope": {
                "envelope_id": ENVELOPE_ID,
                "instance_id": INSTANCE_ID,
                "group_jid": GROUP_JID,
                "actor_jid": ACTOR_JID,
                "wa_message_id": WA_ID,
                "message_timestamp": T_MSG,
                "received_at": T_RECEIVED,
                "text": TEXT,
                "text_truncated": False,
                "quote_message_id": QUOTE_ID,
                "quote_actor_jid": OWNER_JID,
                "attachment_metadata": {
                    "media_type": "application/pdf",
                    "size_bytes": 1024,
                },
            }
        },
        reply={
            "outcome": "captured",
            "event_id": EVENT_ID,
            "envelope_id": ENVELOPE_ID,
            "processing_state": "captured",
            "identity_state": "pending",
        },
        result=lc.IngestReceipt(
            outcome=lc.IngestOutcome.CAPTURED,
            event_id=EVENT_ID,
            envelope_id=ENVELOPE_ID,
            processing_state=lc.ProcessingState.CAPTURED,
            identity_state=lc.IdentityState.PENDING,
        ),
    ),
    Scenario(
        name="recordReply/apply",
        operation="recordReply",
        context=ACTOR_CONTEXT,
        call=lambda c: c.record_reply(actor(), APPLY),
        payload={
            "envelope_id": ENVELOPE_ID,
            "disposition": {"kind": "apply"},
            "correlation": {"quote_message_id": QUOTE_ID, "token": REPLY_TOKEN},
            "patch": {
                "operational_status": "in_progress",
                "next_action": None,
                "next_contact": {
                    "intent": "schedule",
                    "due_at": "2026-12-01T09:00:00-03:00",
                    "timezone": "America/Sao_Paulo",
                    "provenance": "owner_agreed",
                },
            },
        },
        reply={
            "outcome": "applied",
            "document_id": DOC_ID,
            "followup_id": FOLLOWUP_ID,
            "event_ids": [EVENT_ID],
            "revision": 5,
            "agenda_effect": "scheduled",
            "current_next_contact": {"state": "none"},
            "recorded_at": T_UTC,
            "envelope_id": ENVELOPE_ID,
            "identity_state": "transport_attested",
        },
        result=lc.AppliedReceipt(
            outcome=lc.ApplyOutcome.APPLIED,
            document_id=DOC_ID,
            followup_id=FOLLOWUP_ID,
            event_ids=(EVENT_ID,),
            revision=5,
            agenda_effect=lc.AgendaEffect.SCHEDULED,
            current_next_contact=None,
            recorded_at=T_UTC,
            envelope_id=ENVELOPE_ID,
            identity_state=lc.IdentityState.TRANSPORT_ATTESTED,
        ),
    ),
    Scenario(
        name="recordReply/needs_clarification",
        operation="recordReply",
        context=ACTOR_CONTEXT,
        call=lambda c: c.record_reply(
            actor(),
            lc.NeedsClarification(reason=lc.ClarificationReason.MISSING_CORRELATION),
        ),
        payload={
            "envelope_id": ENVELOPE_ID,
            "disposition": {
                "kind": "needs_clarification",
                "reason": "missing_correlation",
            },
        },
        reply={
            "outcome": "needs_clarification",
            "envelope_id": ENVELOPE_ID,
            "event_id": EVENT_ID,
            "processing_state": "needs_clarification",
            "reason": "missing_correlation",
            "recorded_at": T_UTC,
        },
        result=lc.ClarificationReceipt(
            outcome=lc.ClarificationOutcome.NEEDS_CLARIFICATION,
            envelope_id=ENVELOPE_ID,
            event_id=EVENT_ID,
            processing_state=lc.ProcessingState.NEEDS_CLARIFICATION,
            reason=lc.ClarificationReason.MISSING_CORRELATION,
            recorded_at=T_UTC,
        ),
    ),
    Scenario(
        name="queryFollowups",
        operation="queryFollowups",
        context=ACTOR_CONTEXT,
        call=lambda c: c.query_followups(
            actor(),
            lc.FollowupFilter(risk_level=lc.RiskLevel.HIGH),
            limit=2,
            cursor=CURSOR,
        ),
        payload={"filter": {"risk_level": "high"}, "limit": 2, "cursor": CURSOR},
        reply={"items": [QUERY_ITEM], "next_cursor": NEXT_CURSOR},
        result=lc.FollowupPage(
            items=(
                lc.FollowupItem(
                    document_id=DOC_ID,
                    followup_id=FOLLOWUP_ID,
                    revision=4,
                    operational_status=lc.OperationalStatus.IN_PROGRESS,
                    risk_level=lc.RiskLevel.HIGH,
                    owner_display_name="Participante Sintético",
                    identity_state=lc.IdentityState.TRANSPORT_ATTESTED,
                    next_action="Protocolar renovação",
                    next_action_due_at="2026-12-01T12:00:00Z",
                    current_next_contact=lc.ScheduledContact(
                        job_id=JOB_ID, due_at=T_UTC
                    ),
                ),
            ),
            next_cursor=NEXT_CURSOR,
        ),
    ),
    Scenario(
        name="listCaptured",
        operation="listCaptured",
        context=WORKER_CONTEXT,
        call=lambda c: c.list_captured(WORKER, limit=50, cursor=None),
        payload={"limit": 50, "cursor": None},
        reply={"items": [CAPTURED_ITEM], "next_cursor": None},
        result=lc.CapturedPage(
            items=(
                lc.CapturedItem(
                    event_id=EVENT_ID,
                    envelope_id=ENVELOPE_ID,
                    wa_message_id=WA_ID,
                    actor_jid=ACTOR_JID,
                    message_timestamp=T_MSG,
                    text=TEXT,
                    text_truncated=False,
                    quote_message_id=None,
                    quote_actor_jid=None,
                ),
            ),
            next_cursor=None,
        ),
    ),
    Scenario(
        name="claimDue",
        operation="claimDue",
        context=WORKER_CONTEXT,
        call=lambda c: c.claim_due(WORKER, limit=5, lease_seconds=60),
        payload={"limit": 5, "lease_seconds": 60},
        reply={"claims": [CLAIM]},
        result=lc.ClaimResult(
            claims=(
                lc.Claim(
                    job_id=JOB_ID,
                    attempt_id=ATTEMPT_ID,
                    fence=7,
                    lease_until=T_LEASE,
                    followup_id=FOLLOWUP_ID,
                    revision=4,
                    epoch=3,
                    owner_jid=OWNER_JID,
                ),
            ),
            next_window_at=None,
        ),
    ),
    Scenario(
        name="prepareIntent",
        operation="prepareIntent",
        context=WORKER_CONTEXT,
        call=lambda c: c.prepare_intent(WORKER, ATTEMPT, OWNER_MEMBERSHIP),
        payload={
            **ATTEMPT_PAYLOAD,
            "owner_membership": {
                "owner_jid": OWNER_JID,
                "is_current_member": True,
                "checked_at": T_CHECKED,
            },
        },
        reply={
            "attempt_id": ATTEMPT_ID,
            "fence": 7,
            "lease_until": T_LEASE,
            "send_before": T_SEND_BEFORE,
            "intent_hash": INTENT_HASH,
            "group_jid": GROUP_JID,
            "mention_jid": OWNER_JID,
            "text": PROMPT_TEXT,
            "correlation_token": PROMPT_TOKEN,
        },
        result=lc.PreparedIntent(
            attempt_id=ATTEMPT_ID,
            fence=7,
            lease_until=T_LEASE,
            send_before=T_SEND_BEFORE,
            intent_hash=INTENT_HASH,
            group_jid=GROUP_JID,
            mention_jid=OWNER_JID,
            text=PROMPT_TEXT,
            correlation_token=PROMPT_TOKEN,
        ),
    ),
    Scenario(
        name="confirmReceipt/confirmed",
        operation="confirmReceipt",
        context=WORKER_CONTEXT,
        call=lambda c: c.confirm_receipt(WORKER, ATTEMPT, CONFIRMED),
        payload={
            **ATTEMPT_PAYLOAD,
            "receipt": {
                "state": "confirmed",
                "message_ids": list(MESSAGE_IDS),
                "accepted_at": T_OBSERVED,
                "account_binding_hash": BINDING_HASH,
                "group_jid": GROUP_JID,
                "intent_hash": INTENT_HASH,
            },
        },
        reply={
            "outcome": "confirmed",
            "state": "confirmed",
            "event_id": EVENT_ID,
            "message_ids": list(MESSAGE_IDS),
        },
        result=lc.ReceiptConfirmation(
            outcome=lc.ReceiptOutcome.CONFIRMED,
            state=lc.ReceiptState.CONFIRMED,
            event_id=EVENT_ID,
            message_ids=MESSAGE_IDS,
        ),
    ),
    Scenario(
        name="confirmReceipt/delivery_unknown",
        operation="confirmReceipt",
        context=WORKER_CONTEXT,
        call=lambda c: c.confirm_receipt(
            WORKER,
            ATTEMPT,
            lc.DeliveryUnknownReceipt(reason=lc.DeliveryUnknownReason.SEND_TIMEOUT),
        ),
        payload={
            **ATTEMPT_PAYLOAD,
            "receipt": {"state": "delivery_unknown", "reason": "send_timeout"},
        },
        reply={
            "outcome": "recorded_unknown",
            "state": "delivery_unknown",
            "event_id": EVENT_ID,
            "message_ids": [],
        },
        result=lc.ReceiptConfirmation(
            outcome=lc.ReceiptOutcome.RECORDED_UNKNOWN,
            state=lc.ReceiptState.DELIVERY_UNKNOWN,
            event_id=EVENT_ID,
            message_ids=(),
        ),
    ),
]
BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}
ONE_PER_OPERATION = [
    BY_NAME[name]
    for name in (
        "ingestEnvelope",
        "recordReply/apply",
        "queryFollowups",
        "listCaptured",
        "claimDue",
        "prepareIntent",
        "confirmReceipt/confirmed",
    )
]
scenario_param = pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
operation_param = pytest.mark.parametrize(
    "scenario", ONE_PER_OPERATION, ids=lambda s: s.name
)


class Recorder:
    """Transporte falso: registra cada pedido e devolve a resposta programada."""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


def replying(status: int, body: object) -> Recorder:
    return Recorder(lambda _request: httpx.Response(status, json=body))


def raising(error: type[Exception]) -> Recorder:
    def responder(request: httpx.Request) -> httpx.Response:
        raise error("falha sintética", request=request)

    return Recorder(responder)


def make_client(
    recorder: Recorder, clock: Callable[[], float] = lambda: float(NOW)
) -> lc.LicencasClient:
    return lc.LicencasClient(
        load_config(),
        transport=httpx.MockTransport(recorder),
        clock=clock,
    )


def env_without(name: str) -> Callable[[str], str | None]:
    return {key: value for key, value in SECRETS.items() if key != name}.get


def env_with(**overrides: str) -> Callable[[str], str | None]:
    return {**SECRETS, **overrides}.get


# ---------------------------------------------------------------- assinatura


def test_signature_known_vector() -> None:
    """Vetor calculado com openssl, fora do cliente (signature-kat-openssl.txt na evidência)."""
    signature = lc.sign_request(
        hmac_key=HMAC_KEY,
        pathname="/licencas-api",
        key_id="k1",
        timestamp="1800000000",
        nonce="0f" * 16,
        request_id="123e4567-e89b-42d3-a456-426614174000",
        body=b'{"version":1}',
    )
    assert (
        signature == "9cc43448fe0d0720065ee1c42f185d8e5500004882cc75ffb7d879c896f1d045"
    )


@aio
@scenario_param
async def test_headers_and_body_are_exact(scenario: Scenario) -> None:
    recorder = replying(200, scenario.reply)
    async with make_client(recorder) as client:
        await scenario.call(client)
    (request,) = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == ENV["LICENCAS_API_URL"]
    headers = request.headers
    assert headers["content-type"] == "application/json"
    assert headers["x-licencas-capability"] == CAPABILITY
    assert headers["x-licencas-key-id"] == "k1"
    assert headers["x-licencas-timestamp"] == str(NOW)
    assert re.fullmatch(r"(?:[0-9a-f]{2}){16,64}", headers["x-licencas-nonce"])
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        headers["x-licencas-request-id"],
    )
    assert "authorization" not in headers
    assert "apikey" not in headers
    assert "cookie" not in headers
    # Recalculo independente (hashlib/hmac direto) sobre os bytes REALMENTE enviados.
    signed = "\n".join([
        "licencas/v1",
        "POST",
        "/licencas-api",
        "k1",
        str(NOW),
        headers["x-licencas-nonce"],
        headers["x-licencas-request-id"],
        hashlib.sha256(request.content).hexdigest(),
    ])
    assert (
        headers["x-licencas-signature"]
        == hmac.new(HMAC_KEY, signed.encode(), hashlib.sha256).hexdigest()
    )
    expected = {
        "version": 1,
        "operation": scenario.operation,
        "context": scenario.context,
        "payload": scenario.payload,
    }
    assert json.loads(request.content) == expected
    assert request.content == json.dumps(
        expected, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert int(headers["content-length"]) == len(request.content) <= 16 * 1024


@aio
@scenario_param
async def test_typed_result(scenario: Scenario) -> None:
    async with make_client(replying(200, scenario.reply)) as client:
        result = await scenario.call(client)
    assert result == scenario.result
    assert dataclasses.is_dataclass(result) and not isinstance(result, dict)


# ---------------------------------------------------------------- contextos


@aio
async def test_actor_context_has_the_17_keys_and_phone_attestation() -> None:
    recorder = replying(200, BY_NAME["ingestEnvelope"].reply)
    phone = lc.PhoneAttestation(
        e164=PHONE, source="bridge_participant", attested_at=T_CHECKED
    )
    async with make_client(recorder) as client:
        await client.ingest_envelope(actor(phone=phone), ENVELOPE)
    context = json.loads(recorder.requests[0].content)["context"]
    assert context == {
        **ACTOR_CONTEXT,
        "phone_e164": PHONE,
        "phone_attestation_source": "bridge_participant",
        "phone_attested_at": T_CHECKED,
    }
    assert (
        len(context) == 17
        and "request_id" not in context
        and "operation" not in context
    )


@aio
@pytest.mark.parametrize(
    "name", ["listCaptured", "claimDue", "prepareIntent", "confirmReceipt/confirmed"]
)
async def test_worker_context_has_no_actor(name: str) -> None:
    scenario = BY_NAME[name]
    recorder = replying(200, scenario.reply)
    async with make_client(recorder) as client:
        await scenario.call(client)
    context = json.loads(recorder.requests[0].content)["context"]
    assert context == WORKER_CONTEXT
    assert context["actor_is_current_member"] is False
    for key in (
        "actor_jid",
        "actor_display_name",
        "wa_message_id",
        "envelope_id",
        "member_checked_at",
    ):
        assert context[key] is None


@aio
async def test_owner_membership_goes_in_payload_never_in_context() -> None:
    scenario = BY_NAME["prepareIntent"]
    recorder = replying(200, {**scenario.reply, "mention_jid": None})
    async with make_client(recorder) as client:
        await client.prepare_intent(WORKER, ATTEMPT, OWNER_MEMBERSHIP)
        await client.prepare_intent(WORKER, ATTEMPT, None)
    first, second = (json.loads(request.content) for request in recorder.requests)
    assert first["payload"]["owner_membership"]["owner_jid"] == OWNER_JID
    assert OWNER_JID not in json.dumps(first["context"])
    assert first["context"] == WORKER_CONTEXT
    assert (
        "owner_membership" in second["payload"]
        and second["payload"]["owner_membership"] is None
    )


@aio
async def test_membership_not_attested_is_refused_locally() -> None:
    recorder = replying(200, BY_NAME["ingestEnvelope"].reply)
    not_member = lc.MembershipAttestation(is_current_member=False, checked_at=T_CHECKED)
    async with make_client(recorder) as client:
        with pytest.raises(lc.LicencasRequestError) as caught:
            await client.ingest_envelope(actor(membership=not_member), ENVELOPE)
    assert caught.value.code == "membership_not_attested"
    assert caught.value.action is lc.LicencasAction.DO_NOT_RETRY
    assert recorder.requests == []


# ---------------------------------------------------------------- erros tipados

ERROR_CASES = [
    (503, "busy", lc.LicencasBusyError, lc.LicencasAction.RETRY_SAME_ATTEMPT),
    (
        409,
        "replay_unconfirmed",
        lc.LicencasReplayUnconfirmedError,
        lc.LicencasAction.RESEND_FRESH_AUTH,
    ),
    (429, "rate_limited", lc.LicencasRateLimitedError, lc.LicencasAction.BACK_OFF),
    (400, "invalid_request", lc.LicencasRejectedError, lc.LicencasAction.DO_NOT_RETRY),
    (
        400,
        "invalid_correlation",
        lc.LicencasRejectedError,
        lc.LicencasAction.DO_NOT_RETRY,
    ),
    (401, "unauthenticated", lc.LicencasRejectedError, lc.LicencasAction.DO_NOT_RETRY),
    (403, "forbidden_scope", lc.LicencasRejectedError, lc.LicencasAction.DO_NOT_RETRY),
    (
        403,
        "membership_not_attested",
        lc.LicencasRejectedError,
        lc.LicencasAction.DO_NOT_RETRY,
    ),
    (
        404,
        "capture_not_found",
        lc.LicencasRejectedError,
        lc.LicencasAction.DO_NOT_RETRY,
    ),
    (409, "conflict", lc.LicencasRejectedError, lc.LicencasAction.DO_NOT_RETRY),
    (
        409,
        "correlation_stale",
        lc.LicencasRejectedError,
        lc.LicencasAction.DO_NOT_RETRY,
    ),
    (409, "delivery_unknown", lc.LicencasRejectedError, lc.LicencasAction.DO_NOT_RETRY),
    (409, "window_closed", lc.LicencasRejectedError, lc.LicencasAction.DO_NOT_RETRY),
    (409, "nonce_reused", lc.LicencasRejectedError, lc.LicencasAction.DO_NOT_RETRY),
    (
        413,
        "payload_too_large",
        lc.LicencasRejectedError,
        lc.LicencasAction.DO_NOT_RETRY,
    ),
    (503, "unavailable", lc.LicencasUnavailableError, lc.LicencasAction.UNAVAILABLE),
    (
        503,
        "invalid_rpc_response",
        lc.LicencasUnavailableError,
        lc.LicencasAction.UNAVAILABLE,
    ),
    (
        503,
        "cursor_unavailable",
        lc.LicencasUnavailableError,
        lc.LicencasAction.UNAVAILABLE,
    ),
]


@aio
@pytest.mark.parametrize(("status", "code", "error_type", "action"), ERROR_CASES)
async def test_typed_error_by_status_and_code(
    status: int,
    code: str,
    error_type: type[lc.LicencasError],
    action: lc.LicencasAction,
) -> None:
    recorder = replying(status, {"error": code})
    async with make_client(recorder) as client:
        with pytest.raises(lc.LicencasError) as caught:
            await client.claim_due(WORKER, limit=5, lease_seconds=60)
    error = caught.value
    assert type(error) is error_type
    assert (error.status, error.code, error.action, error.operation) == (
        status,
        code,
        action,
        "claimDue",
    )
    assert error.request_id == recorder.requests[0].headers["x-licencas-request-id"]
    # Um pedido por chamada: nenhum retry escondido, nem para busy/replay/429.
    assert len(recorder.requests) == 1


@aio
@pytest.mark.parametrize(
    ("raised", "error_type", "code", "action"),
    [
        (
            httpx.ReadTimeout,
            lc.LicencasOutcomeUnknownError,
            "timeout",
            lc.LicencasAction.OUTCOME_UNKNOWN,
        ),
        (
            httpx.WriteTimeout,
            lc.LicencasOutcomeUnknownError,
            "timeout",
            lc.LicencasAction.OUTCOME_UNKNOWN,
        ),
        (
            httpx.ReadError,
            lc.LicencasOutcomeUnknownError,
            "network_error",
            lc.LicencasAction.OUTCOME_UNKNOWN,
        ),
        (
            httpx.RemoteProtocolError,
            lc.LicencasOutcomeUnknownError,
            "network_error",
            lc.LicencasAction.OUTCOME_UNKNOWN,
        ),
        (
            httpx.ConnectError,
            lc.LicencasNotSentError,
            "connect_failed",
            lc.LicencasAction.BACK_OFF,
        ),
        (
            httpx.ConnectTimeout,
            lc.LicencasNotSentError,
            "connect_failed",
            lc.LicencasAction.BACK_OFF,
        ),
    ],
)
async def test_transport_failures(
    raised: type[Exception],
    error_type: type[lc.LicencasError],
    code: str,
    action: lc.LicencasAction,
) -> None:
    recorder = raising(raised)
    async with make_client(recorder) as client:
        with pytest.raises(lc.LicencasError) as caught:
            await client.confirm_receipt(WORKER, ATTEMPT, CONFIRMED)
    error = caught.value
    assert type(error) is error_type
    assert (error.status, error.code, error.action) == (None, code, action)
    assert len(recorder.requests) == 1
    # A exceção do httpx (que carrega o pedido e os cabeçalhos) não fica encadeada.
    assert error.__cause__ is None and error.__suppress_context__ is True
    for secret in SENSITIVE:
        assert secret not in str(error) and secret not in repr(error)


@aio
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_redirect_is_refused_not_followed(status: int) -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(
            status, headers={"location": "https://outro.invalid/licencas-api"}
        )
    )
    async with make_client(recorder) as client:
        with pytest.raises(lc.LicencasResponseError) as caught:
            await client.claim_due(WORKER, limit=5, lease_seconds=60)
    assert caught.value.code == "redirect_refused"
    assert caught.value.action is lc.LicencasAction.OUTCOME_UNKNOWN
    assert len(recorder.requests) == 1


@aio
@pytest.mark.parametrize("status", [201, 204, 418, 500, 502, 504])
async def test_status_outside_closed_set_is_unknown(status: int) -> None:
    recorder = replying(status, {"error": "unavailable"})
    async with make_client(recorder) as client:
        with pytest.raises(lc.LicencasResponseError) as caught:
            await client.claim_due(WORKER, limit=5, lease_seconds=60)
    assert caught.value.code == "unexpected_status"
    assert isinstance(caught.value, lc.LicencasOutcomeUnknownError)


@aio
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b'{"error":"busy","extra":1}',
        b'{"error":7}',
        b'{"error":"Bad Code"}',
        b"[]",
        b'{"error":"a","error":"busy"}',
    ],
)
async def test_error_body_outside_contract_is_unknown(body: bytes) -> None:
    recorder = Recorder(lambda _request: httpx.Response(503, content=body))
    async with make_client(recorder) as client:
        with pytest.raises(lc.LicencasResponseError) as caught:
            await client.claim_due(WORKER, limit=5, lease_seconds=60)
    assert caught.value.code == "invalid_response"
    assert len(recorder.requests) == 1


# ---------------------------------------------------------------- respostas malformadas


def _mutations(reply: dict[str, object]) -> list[tuple[str, object]]:
    first = next(iter(reply))
    return [
        ("chave extra", {**reply, "unexpected": 1}),
        (
            "chave faltante",
            {key: value for key, value in reply.items() if key != first},
        ),
        ("tipo errado", {**reply, first: 12345}),
        ("não é objeto", [reply]),
    ]


MALFORMED = [
    (scenario, label, body)
    for scenario in SCENARIOS
    for label, body in _mutations(scenario.reply)
] + [
    (
        BY_NAME["ingestEnvelope"],
        "envelope de outro pedido",
        {**BY_NAME["ingestEnvelope"].reply, "envelope_id": "ee" * 32},
    ),
    (
        BY_NAME["ingestEnvelope"],
        "enum desconhecido",
        {**BY_NAME["ingestEnvelope"].reply, "outcome": "applied"},
    ),
    (
        BY_NAME["recordReply/apply"],
        "recibo de esclarecimento no apply",
        BY_NAME["recordReply/needs_clarification"].reply,
    ),
    (
        BY_NAME["recordReply/needs_clarification"],
        "recibo applied no esclarecimento",
        BY_NAME["recordReply/apply"].reply,
    ),
    (
        BY_NAME["recordReply/needs_clarification"],
        "motivo diferente do pedido",
        {
            **BY_NAME["recordReply/needs_clarification"].reply,
            "reason": "patch_rejected",
        },
    ),
    (
        BY_NAME["recordReply/apply"],
        "event_ids repetidos",
        {**BY_NAME["recordReply/apply"].reply, "event_ids": [EVENT_ID] * 2},
    ),
    (
        BY_NAME["recordReply/apply"],
        "revision booleana",
        {**BY_NAME["recordReply/apply"].reply, "revision": True},
    ),
    (
        BY_NAME["recordReply/apply"],
        "revision fracionária",
        {**BY_NAME["recordReply/apply"].reply, "revision": 5.5},
    ),
    (
        BY_NAME["recordReply/apply"],
        "next_contact com chave extra",
        {
            **BY_NAME["recordReply/apply"].reply,
            "current_next_contact": {"state": "none", "job_id": JOB_ID},
        },
    ),
    (
        BY_NAME["queryFollowups"],
        "item com chave extra",
        {"items": [{**QUERY_ITEM, "x": 1}], "next_cursor": None},
    ),
    (
        BY_NAME["queryFollowups"],
        "mais itens que o limite",
        {"items": [QUERY_ITEM] * 3, "next_cursor": None},
    ),
    (
        BY_NAME["queryFollowups"],
        "cursor fora da forma",
        {"items": [], "next_cursor": "tem espaço"},
    ),
    (
        BY_NAME["queryFollowups"],
        "next_position cru da RPC",
        {"items": [], "next_position": None},
    ),
    (
        BY_NAME["listCaptured"],
        "item sem texto",
        {
            "items": [{k: v for k, v in CAPTURED_ITEM.items() if k != "text"}],
            "next_cursor": None,
        },
    ),
    (
        BY_NAME["listCaptured"],
        "truncado não booleano",
        {"items": [{**CAPTURED_ITEM, "text_truncated": 0}], "next_cursor": None},
    ),
    (
        BY_NAME["claimDue"],
        "claim sem owner_jid",
        {"claims": [{k: v for k, v in CLAIM.items() if k != "owner_jid"}]},
    ),
    (
        BY_NAME["claimDue"],
        "epoch de outra configuração",
        {"claims": [{**CLAIM, "epoch": 4}]},
    ),
    (BY_NAME["claimDue"], "fence zero", {"claims": [{**CLAIM, "fence": 0}]}),
    (BY_NAME["claimDue"], "tentativa repetida", {"claims": [CLAIM, CLAIM]}),
    (
        BY_NAME["claimDue"],
        "next_window_at com claims",
        {"claims": [CLAIM], "next_window_at": T_UTC},
    ),
    (
        BY_NAME["claimDue"],
        "lease sem microssegundos",
        {"claims": [{**CLAIM, "lease_until": T_MSG}]},
    ),
    (
        BY_NAME["prepareIntent"],
        "outra tentativa",
        {**BY_NAME["prepareIntent"].reply, "attempt_id": JOB_ID},
    ),
    (
        BY_NAME["prepareIntent"],
        "outro fence",
        {**BY_NAME["prepareIntent"].reply, "fence": 8},
    ),
    (
        BY_NAME["prepareIntent"],
        "outro grupo",
        {**BY_NAME["prepareIntent"].reply, "group_jid": "120363000000000002@g.us"},
    ),
    (
        BY_NAME["prepareIntent"],
        "send_before depois da lease",
        {
            **BY_NAME["prepareIntent"].reply,
            "send_before": "2026-11-27T12:01:06.000001Z",
        },
    ),
    (
        BY_NAME["prepareIntent"],
        "texto vazio",
        {**BY_NAME["prepareIntent"].reply, "text": ""},
    ),
    (
        BY_NAME["prepareIntent"],
        "data inexistente",
        {
            **BY_NAME["prepareIntent"].reply,
            "lease_until": "2026-02-30T12:01:06.000000Z",
        },
    ),
    (
        BY_NAME["confirmReceipt/confirmed"],
        "IDs em outra ordem",
        {
            **BY_NAME["confirmReceipt/confirmed"].reply,
            "message_ids": list(MESSAGE_IDS[::-1]),
        },
    ),
    (
        BY_NAME["confirmReceipt/confirmed"],
        "incerteza no lugar de confirmado",
        BY_NAME["confirmReceipt/delivery_unknown"].reply,
    ),
    (
        BY_NAME["confirmReceipt/delivery_unknown"],
        "confirmado no lugar de incerteza",
        BY_NAME["confirmReceipt/confirmed"].reply,
    ),
]


@aio
@pytest.mark.parametrize(
    ("scenario", "label", "body"),
    MALFORMED,
    ids=lambda v: v.name if isinstance(v, Scenario) else None,
)
async def test_malformed_response_is_a_typed_error(
    scenario: Scenario, label: str, body: object
) -> None:
    recorder = replying(200, body)
    async with make_client(recorder) as client:
        with pytest.raises(lc.LicencasResponseError) as caught:
            await scenario.call(client)
    error = caught.value
    assert (error.status, error.code, error.action) == (
        200,
        "invalid_response",
        lc.LicencasAction.OUTCOME_UNKNOWN,
    ), label
    assert error.operation == scenario.operation
    assert len(recorder.requests) == 1
    for secret in SENSITIVE:
        assert secret not in str(error), label


@aio
@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b'{"claims":[],"claims":[]}',
        b'{"claims":[],"next_window_at":NaN}',
        b'{"claims":[]} trailing',
        b"\xff\xfe",
        b'\xef\xbb\xbf{"claims":[]}',
    ],
)
async def test_response_json_is_strict(raw: bytes) -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, content=raw))
    async with make_client(recorder) as client:
        with pytest.raises(lc.LicencasResponseError):
            await client.claim_due(WORKER, limit=5, lease_seconds=60)


@aio
async def test_claim_due_closed_window_and_null_owner() -> None:
    async with make_client(
        replying(200, {"claims": [], "next_window_at": T_UTC})
    ) as client:
        closed = await client.claim_due(WORKER, limit=5, lease_seconds=60)
    assert closed == lc.ClaimResult(claims=(), next_window_at=T_UTC)
    async with make_client(
        replying(200, {"claims": [{**CLAIM, "owner_jid": None}]})
    ) as client:
        unowned = await client.claim_due(WORKER, limit=5, lease_seconds=60)
    assert unowned.claims[0].owner_jid is None


# ---------------------------------------------------------------- recusas locais (zero pedidos)

LOCAL_REFUSALS: list[tuple[str, Call, str]] = [
    (
        "NaN no limite",
        lambda c: c.claim_due(WORKER, limit=float("nan"), lease_seconds=60),
        "invalid_input",
    ),
    (
        "Infinity no fence",
        lambda c: c.prepare_intent(
            WORKER, lc.AttemptRef(JOB_ID, ATTEMPT_ID, float("inf")), None
        ),
        "invalid_input",
    ),
    (
        "NaN no anexo",
        lambda c: c.ingest_envelope(
            actor(),
            dataclasses.replace(
                ENVELOPE,
                attachment_metadata=lc.AttachmentMetadata(
                    "application/pdf", float("nan")
                ),
            ),
        ),
        "invalid_input",
    ),
    (
        "corpo acima de 16 KiB",
        lambda c: c.ingest_envelope(
            actor(), dataclasses.replace(ENVELOPE, text="x" * 16_384)
        ),
        "payload_too_large",
    ),
    (
        "surrogate isolado",
        lambda c: c.ingest_envelope(
            actor(), dataclasses.replace(ENVELOPE, text="\ud800")
        ),
        "invalid_json",
    ),
    (
        "booleano como inteiro",
        lambda c: c.claim_due(WORKER, limit=True, lease_seconds=60),
        "invalid_input",
    ),
    (
        "limite fora da faixa",
        lambda c: c.claim_due(WORKER, limit=21, lease_seconds=60),
        "invalid_input",
    ),
    (
        "lease fora da faixa",
        lambda c: c.claim_due(WORKER, limit=5, lease_seconds=9),
        "invalid_input",
    ),
    (
        "fence acima de 2^53-1",
        lambda c: c.prepare_intent(
            WORKER, lc.AttemptRef(JOB_ID, ATTEMPT_ID, 2**53), None
        ),
        "invalid_input",
    ),
    (
        "JID de ator inválido",
        lambda c: c.ingest_envelope(actor(actor_jid="5511@g.us"), ENVELOPE),
        "invalid_input",
    ),
    (
        "envelope_id não hex",
        lambda c: c.ingest_envelope(actor(envelope_id="XYZ"), ENVELOPE),
        "invalid_input",
    ),
    (
        "instante sem fuso",
        lambda c: c.ingest_envelope(actor(observed_at="2026-11-27T12:00:05"), ENVELOPE),
        "invalid_input",
    ),
    (
        "instante com quebra de linha",
        lambda c: c.list_captured(
            lc.WorkerContext(T_OBSERVED + "\n"), limit=5, cursor=None
        ),
        "invalid_input",
    ),
    (
        "nome com caractere de controle",
        lambda c: c.ingest_envelope(actor(actor_display_name="a\x07b"), ENVELOPE),
        "invalid_input",
    ),
    (
        "contexto de worker em operação de ator",
        lambda c: c.ingest_envelope(WORKER, ENVELOPE),
        "invalid_input",
    ),
    (
        "contexto de ator em operação de worker",
        lambda c: c.claim_due(actor(), limit=5, lease_seconds=60),
        "invalid_input",
    ),
    (
        "correlação vazia",
        lambda c: c.record_reply(
            actor(), lc.ApplyReply(lc.ReplyCorrelation(None, None), APPLY.patch)
        ),
        "invalid_input",
    ),
    (
        "patch vazio",
        lambda c: c.record_reply(
            actor(), lc.ApplyReply(APPLY.correlation, lc.ReplyPatch())
        ),
        "invalid_input",
    ),
    (
        "motivo fora do enum",
        lambda c: c.record_reply(actor(), lc.NeedsClarification("qualquer")),
        "invalid_input",
    ),
    (
        "disposição desconhecida",
        lambda c: c.record_reply(actor(), {"kind": "apply"}),
        "invalid_input",
    ),
    (
        "filtro com enum cru",
        lambda c: c.query_followups(
            actor(), lc.FollowupFilter(risk_level="high"), limit=5, cursor=None
        ),
        "invalid_input",
    ),
    (
        "cursor fora da forma",
        lambda c: c.list_captured(WORKER, limit=5, cursor="tem espaço"),
        "invalid_input",
    ),
    (
        "recibo sem IDs",
        lambda c: c.confirm_receipt(
            WORKER, ATTEMPT, dataclasses.replace(CONFIRMED, message_ids=())
        ),
        "invalid_input",
    ),
    (
        "recibo com ID repetido",
        lambda c: c.confirm_receipt(
            WORKER,
            ATTEMPT,
            dataclasses.replace(
                CONFIRMED, message_ids=(MESSAGE_IDS[0], MESSAGE_IDS[0])
            ),
        ),
        "invalid_input",
    ),
    (
        "atestado do responsável com JID inválido",
        lambda c: c.prepare_intent(
            WORKER,
            ATTEMPT,
            dataclasses.replace(OWNER_MEMBERSHIP, owner_jid="alguem@lid"),
        ),
        "invalid_input",
    ),
    (
        "atestado do responsável sem segundos",
        lambda c: c.prepare_intent(
            WORKER,
            ATTEMPT,
            dataclasses.replace(OWNER_MEMBERSHIP, checked_at="2026-11-27T12:00Z"),
        ),
        "invalid_input",
    ),
]


@aio
@pytest.mark.parametrize(
    ("label", "call", "code"), LOCAL_REFUSALS, ids=[case[0] for case in LOCAL_REFUSALS]
)
async def test_refused_before_sending(label: str, call: Call, code: str) -> None:
    recorder = replying(200, {})
    async with make_client(recorder) as client:
        with pytest.raises(lc.LicencasRequestError) as caught:
            await call(client)
    assert caught.value.code == code, label
    assert (
        caught.value.status is None
        and caught.value.action is lc.LicencasAction.DO_NOT_RETRY
    )
    assert recorder.requests == []


@aio
async def test_largest_accepted_body_is_exactly_16_kib() -> None:
    recorder = replying(200, BY_NAME["ingestEnvelope"].reply)
    base = len(
        json.dumps(
            {
                "version": 1,
                "operation": "ingestEnvelope",
                "context": ACTOR_CONTEXT,
                "payload": BY_NAME["ingestEnvelope"].payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ) - len(json.dumps(TEXT, ensure_ascii=False).encode("utf-8"))
    fits = "x" * (16 * 1024 - base - 2)
    async with make_client(recorder) as client:
        await client.ingest_envelope(actor(), dataclasses.replace(ENVELOPE, text=fits))
        with pytest.raises(lc.LicencasRequestError) as caught:
            await client.ingest_envelope(
                actor(), dataclasses.replace(ENVELOPE, text=fits + "x")
            )
    assert caught.value.code == "payload_too_large"
    assert [len(request.content) for request in recorder.requests] == [16 * 1024]


# ---------------------------------------------------------------- configuração (falha fechada)


def test_config_loads_into_closed_type() -> None:
    config = load_config()
    assert dataclasses.is_dataclass(config) and config.__dataclass_params__.frozen
    assert (
        config.endpoint == ENV["LICENCAS_API_URL"]
        and config.signed_pathname == "/licencas-api"
    )
    assert config.hmac_key == HMAC_KEY and config.policy_epoch == 3
    assert set(lc.LICENCAS_SECRET_ENV_NAMES) == set(SECRETS)
    assert set(lc.LICENCAS_YAML_KEYS) == set(YAML_REQUIRED)
    shown = repr(config)
    assert (
        CAPABILITY not in shown
        and ENV["LICENCAS_HMAC_KEY_B64"] not in shown
        and repr(HMAC_KEY) not in shown
    )


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_missing_or_empty_secret_fails_closed(name: str) -> None:
    for reader in (env_without(name), env_with(**{name: ""})):
        with pytest.raises(lc.LicencasConfigError) as caught:
            lc.LicencasClient.from_env(reader, raw_config=RAW_CONFIG)
        assert caught.value.field == name
        assert ENV[name] not in str(caught.value)


_YAML_OF = {
    "LICENCAS_API_URL": "api_url",
    "LICENCAS_API_SIGNED_PATHNAME": "signed_pathname",
    "LICENCAS_KEY_ID": "key_id",
    "LICENCAS_INSTANCE_ID": "instance_id",
    "LICENCAS_POLICY_ID": "policy_id",
    "LICENCAS_GROUP_JID": "group_jid",
    "LICENCAS_ACCOUNT_BINDING_HASH": "account_binding_hash",
}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LICENCAS_API_URL", "http://licencas.invalid/licencas-api"),
        ("LICENCAS_API_URL", "http://127.0.0.1:8787/licencas-api"),
        ("LICENCAS_API_URL", "https://licencas.invalid/licencas-api?x=1"),
        ("LICENCAS_API_URL", "https://licencas.invalid/licencas-api?"),
        ("LICENCAS_API_URL", "https://licencas.invalid/licencas-api#frag"),
        ("LICENCAS_API_URL", "https://user:pw@licencas.invalid/licencas-api"),
        ("LICENCAS_API_URL", "https://licencas.invalid/outra-funcao"),
        ("LICENCAS_API_URL", "https://licencas.invalid/x-licencas-api"),
        ("LICENCAS_API_URL", "https://licencas.invalid/licencas-api/"),
        ("LICENCAS_API_URL", "HTTPS://licencas.invalid/licencas-api"),
        ("LICENCAS_API_URL", " https://licencas.invalid/licencas-api"),
        ("LICENCAS_API_URL", "https://licencas.invalid/licencas-api\n"),
        ("LICENCAS_API_URL", "https:///licencas-api"),
        ("LICENCAS_API_URL", "https://[::1/licencas-api"),
        ("LICENCAS_API_URL", "https://licencas.invalid:99999/licencas-api"),
        ("LICENCAS_API_URL", "https://licencas.invalid:0/licencas-api"),
        ("LICENCAS_API_SIGNED_PATHNAME", "licencas-api"),
        ("LICENCAS_API_SIGNED_PATHNAME", "/licencas-api?x=1"),
        ("LICENCAS_KEY_ID", "tem espaço"),
        ("LICENCAS_INSTANCE_ID", "sem-arroba"),
        ("LICENCAS_POLICY_ID", POLICY_ID.upper()),
        ("LICENCAS_GROUP_JID", "5511999990000@s.whatsapp.net"),
        ("LICENCAS_ACCOUNT_BINDING_HASH", "AB" * 32),
    ],
)
def test_invalid_public_option_fails_closed(name: str, value: str) -> None:
    key = _YAML_OF[name]
    raw = {"licencas_followup": {**YAML_SECTION, key: value}}
    with pytest.raises(lc.LicencasConfigError) as caught:
        lc.load_licencas_config(SECRETS.get, raw_config=raw)
    assert caught.value.field == key
    assert value not in str(caught.value) or value == ""


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LICENCAS_CAPABILITY", "curta"),
        ("LICENCAS_CAPABILITY", "eyJ" + "a" * 40),
        ("LICENCAS_CAPABILITY", "aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb.cccccccccccccccc"),
        ("LICENCAS_CAPABILITY", "sb_" + "a" * 40),
        ("LICENCAS_HMAC_KEY_B64", base64.b64encode(bytes(31)).decode("ascii")),
        ("LICENCAS_HMAC_KEY_B64", base64.b64encode(bytes(129)).decode("ascii")),
        ("LICENCAS_HMAC_KEY_B64", "não-é-base64"),
        ("LICENCAS_HMAC_KEY_B64", ENV["LICENCAS_HMAC_KEY_B64"] + "\n"),
    ],
)
def test_invalid_secret_fails_closed(name: str, value: str) -> None:
    with pytest.raises(lc.LicencasConfigError) as caught:
        load_config(env_with(**{name: value}))
    assert caught.value.field == name
    assert value not in str(caught.value) or value == ""


def test_config_type_validates_direct_construction() -> None:
    config = load_config()
    with pytest.raises(lc.LicencasConfigError):
        dataclasses.replace(config, endpoint="http://127.0.0.1:8787/licencas-api")
    with pytest.raises(lc.LicencasConfigError):
        dataclasses.replace(config, hmac_key=b"curta")


def test_default_reader_is_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(lc.LicencasConfigError):
        lc.LicencasClient.from_env(raw_config=RAW_CONFIG)
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    assert lc.load_licencas_config(raw_config=RAW_CONFIG) == load_config()


# ---------------------------------------------------------------- configuração a partir do config.yaml real


def section_text(**changes: object) -> str:
    section = {**YAML_SECTION, **changes}
    lines = [f"  {key}: {json.dumps(value)}" for key, value in section.items()]
    return "licencas_followup:\n" + "\n".join(lines) + "\n"


class SecretsSpy:
    """Leitor injetado: só os dois segredos existem; qualquer outro nome é registrado e falha."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def __call__(self, name: str) -> str | None:
        self.asked.append(name)
        return SECRETS.get(name)


@pytest.fixture
def base_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def write_config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_loader_reads_the_real_config_yaml_and_only_the_two_secrets(
    base_home: Path,
) -> None:
    write_config(base_home, section_text())
    spy = SecretsSpy()
    config = lc.load_licencas_config(spy)
    assert config == load_config()
    assert sorted(spy.asked) == sorted(SECRET_NAMES)


def test_from_env_reads_yaml_even_with_conflicting_environment(
    base_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(base_home, section_text())
    for name in ENV:
        if name not in SECRET_NAMES:
            monkeypatch.setenv(name, "https://conflito.invalid/outro")
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    for reader in (SecretsSpy(), os.environ.get):
        client = lc.LicencasClient.from_env(reader)
        assert client._config == load_config()


@pytest.mark.parametrize("name", YAML_REQUIRED)
def test_yaml_missing_or_empty_option_fails_before_credentials(
    base_home: Path, name: str
) -> None:
    section = {k: v for k, v in YAML_SECTION.items() if k != name}
    for raw in (section, {**section, name: ""}, {**section, name: None}):
        spy = SecretsSpy()
        with pytest.raises(lc.LicencasConfigError) as caught:
            lc.load_licencas_config(spy, raw_config={"licencas_followup": raw})
        assert caught.value.field == name
        assert spy.asked == []


@pytest.mark.parametrize(
    "epoch", [True, False, "3", 3.0, 0, -1, 2**53, None, [3], {"a": 3}]
)
def test_yaml_policy_epoch_must_be_a_strict_int(epoch: object) -> None:
    spy = SecretsSpy()
    raw = {"licencas_followup": {**YAML_SECTION, "policy_epoch": epoch}}
    with pytest.raises(lc.LicencasConfigError) as caught:
        lc.load_licencas_config(spy, raw_config=raw)
    assert caught.value.field == "policy_epoch" and spy.asked == []


def test_yaml_policy_epoch_accepts_the_range_edges() -> None:
    for epoch in (1, lc.MAX_SAFE_INTEGER):
        raw = {"licencas_followup": {**YAML_SECTION, "policy_epoch": epoch}}
        assert (
            lc.load_licencas_config(SecretsSpy(), raw_config=raw).policy_epoch == epoch
        )


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("api_url", "http://licencas.invalid/licencas-api"),
        ("api_url", 7),
        ("signed_pathname", "licencas-api"),
        ("key_id", "tem espaço"),
        ("principal_id", ["x"]),
        ("instance_id", "sem-arroba"),
        ("policy_id", POLICY_ID.upper()),
        ("group_jid", "5511999990000@s.whatsapp.net"),
        ("account_binding_hash", "AB" * 32),
        ("enabled", "yes"),
    ],
)
def test_yaml_values_keep_every_existing_validator(key: str, bad: object) -> None:
    spy = SecretsSpy()
    with pytest.raises(lc.LicencasConfigError) as caught:
        lc.load_licencas_config(
            spy, raw_config={"licencas_followup": {**YAML_SECTION, key: bad}}
        )
    assert caught.value.field == key and spy.asked == []
    assert str(bad) not in str(caught.value) or str(bad) == key


@pytest.mark.parametrize(
    "raw",
    [None, [], "texto", 3, {}, {"licencas_followup": None}, {"licencas_followup": []}],
)
def test_invalid_raw_config_structure_fails_closed(raw: object) -> None:
    spy = SecretsSpy()
    with pytest.raises(lc.LicencasConfigError):
        lc.load_licencas_config(spy, raw_config=raw)
    assert spy.asked == []


def test_explicit_none_raw_config_never_falls_back_to_disk(base_home: Path) -> None:
    write_config(base_home, section_text())
    with pytest.raises(lc.LicencasConfigError):
        lc.load_licencas_config(SecretsSpy(), raw_config=None)


def test_raw_config_injection_does_not_touch_the_disk(base_home: Path) -> None:
    write_config(base_home, "licencas_followup: [yaml quebrado\n")
    config = lc.load_licencas_config(
        SecretsSpy(), raw_config={"licencas_followup": dict(YAML_SECTION)}
    )
    assert config == load_config()


@pytest.mark.parametrize(
    "text",
    [
        "licencas_followup: [yaml quebrado\n",
        section_text() + section_text(),
        section_text().replace("  key_id", "  principal_id: dup\n  key_id", 1),
        "base: &a\n  key_id: k1\nlicencas_followup:\n  <<: *a\n  api_url: x\n",
        "x: &s\n  group_jid: '1'\nlicencas_followup: *s\n",
        "- lista\n- na raiz\n",
        "",
    ],
)
def test_yaml_file_that_is_not_one_explicit_section_fails_closed(
    base_home: Path, text: str
) -> None:
    write_config(base_home, text)
    spy = SecretsSpy()
    with pytest.raises(lc.LicencasConfigError):
        lc.load_licencas_config(spy)
    assert spy.asked == []


def test_missing_config_file_and_io_error_fail_closed(base_home: Path) -> None:
    spy = SecretsSpy()
    with pytest.raises(lc.LicencasConfigError):
        lc.load_licencas_config(spy)
    (base_home / "config.yaml").mkdir()
    with pytest.raises(lc.LicencasConfigError):
        lc.load_licencas_config(spy)
    assert spy.asked == []


def test_base_profile_ignores_a_context_override(
    base_home: Path, tmp_path: Path
) -> None:
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    write_config(base_home, section_text())
    other = tmp_path / "outro-perfil"
    other.mkdir()
    write_config(other, section_text(key_id="outro"))
    token = set_hermes_home_override(other)
    try:
        assert lc.load_licencas_config(SecretsSpy()).key_id == "k1"
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize(
    "key", ["capability", "hmac_key", "HMAC_KEY_B64", "licencas_capability"]
)
def test_secret_key_in_yaml_is_refused_without_echoing_it(key: str) -> None:
    spy = SecretsSpy()
    raw = {"licencas_followup": {**YAML_SECTION, key: "valor-secreto-sintetico"}}
    with pytest.raises(lc.LicencasConfigError) as caught:
        lc.load_licencas_config(spy, raw_config=raw)
    assert caught.value.field == "secret_in_config" and spy.asked == []
    assert "valor-secreto-sintetico" not in str(caught.value)


def test_unknown_yaml_key_is_refused() -> None:
    raw = {"licencas_followup": {**YAML_SECTION, "outra": "x"}}
    with pytest.raises(lc.LicencasConfigError):
        lc.load_licencas_config(SecretsSpy(), raw_config=raw)


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_credential_reader_failures_are_sanitized(name: str) -> None:
    raw = {"licencas_followup": dict(YAML_SECTION)}

    def exploding(_name: str) -> str | None:
        raise RuntimeError("segredo-no-erro-sintetico")

    cases: list[Callable[[str], object]] = [
        exploding,
        lambda n: 123 if n == name else SECRETS[n],
        lambda n: None if n == name else SECRETS[n],
        lambda n: "" if n == name else SECRETS[n],
    ]
    for reader in cases:
        with pytest.raises(lc.LicencasConfigError) as caught:
            lc.load_licencas_config(
                cast(Callable[[str], str | None], reader), raw_config=raw
            )
        error = caught.value
        assert error.__cause__ is None and error.__context__ is None
        assert "segredo-no-erro-sintetico" not in str(error) + repr(error.args)
        assert error.field in SECRET_NAMES


def test_config_errors_and_logs_never_carry_public_values(
    base_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_config(
        base_home, section_text(api_url="http://licencas.invalid/licencas-api")
    )
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(lc.LicencasConfigError) as caught,
    ):
        lc.load_licencas_config(SecretsSpy())
    shown = str(caught.value) + caplog.text
    for value in (GROUP_JID, INSTANCE_ID, BINDING_HASH, "licencas.invalid"):
        assert value not in shown


# ---------------------------------------------------------------- transporte


@aio
async def test_http_client_is_locked_down() -> None:
    client = lc.LicencasClient(load_config())
    try:
        http = client._http
        assert http.follow_redirects is False
        assert http.trust_env is False
        timeout = http.timeout
        assert all(
            isinstance(value, float) and 0 < value <= 30
            for value in (timeout.connect, timeout.read, timeout.write, timeout.pool)
        )
        context = http._transport._pool._ssl_context
        assert (
            context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname is True
        )
        assert http._mounts == {}
    finally:
        await client.aclose()


@aio
async def test_proxy_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    client = lc.LicencasClient(load_config())
    try:
        assert client._http._mounts == {}
    finally:
        await client.aclose()


@aio
async def test_loopback_only_through_explicit_test_parameter() -> None:
    recorder = replying(200, BY_NAME["claimDue"].reply)
    config = load_config()
    client = lc.LicencasClient(
        config,
        transport=httpx.MockTransport(recorder),
        clock=lambda: float(NOW),
        loopback_test_port=18787,
    )
    async with client:
        await client.claim_due(WORKER, limit=5, lease_seconds=60)
    assert str(recorder.requests[0].url) == "http://127.0.0.1:18787/licencas-api"
    # Nenhum nome de variável de ambiente nem campo de configuração leva ao loopback.
    assert not any(
        "LOOPBACK" in name or "TEST" in name
        for name in (*lc.LICENCAS_SECRET_ENV_NAMES, *lc.LICENCAS_YAML_KEYS)
    )
    assert "loopback_test_port" not in {
        field.name for field in dataclasses.fields(config)
    }
    with pytest.raises(TypeError):
        lc.LicencasClient.from_env(SECRETS.get, loopback_test_port=18787)
    for port in (0, 70000, True, "8787"):
        with pytest.raises(lc.LicencasConfigError):
            lc.LicencasClient(config, loopback_test_port=port)


@aio
async def test_cookies_are_never_stored_or_sent() -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(
            200,
            json=BY_NAME["claimDue"].reply,
            headers={"set-cookie": "sessao=valor; Path=/"},
        )
    )
    async with make_client(recorder) as client:
        await client.claim_due(WORKER, limit=5, lease_seconds=60)
        await client.claim_due(WORKER, limit=5, lease_seconds=60)
        assert len(client._http.cookies.jar) == 0
    assert all("cookie" not in request.headers for request in recorder.requests)


@aio
async def test_nonce_and_request_id_are_fresh_on_every_request() -> None:
    recorder = replying(200, BY_NAME["claimDue"].reply)
    async with make_client(recorder) as client:
        for _ in range(25):
            await client.claim_due(WORKER, limit=5, lease_seconds=60)
    nonces = {request.headers["x-licencas-nonce"] for request in recorder.requests}
    request_ids = {
        request.headers["x-licencas-request-id"] for request in recorder.requests
    }
    assert len(nonces) == len(request_ids) == len(recorder.requests) == 25
    assert all(len(bytes.fromhex(nonce)) * 8 >= 128 for nonce in nonces)


@aio
async def test_timestamp_comes_from_the_clock_at_each_request() -> None:
    ticks = iter([1_800_000_000.9, 1_800_000_061.2])
    recorder = replying(200, BY_NAME["claimDue"].reply)
    async with make_client(recorder, clock=lambda: next(ticks)) as client:
        await client.claim_due(WORKER, limit=5, lease_seconds=60)
        await client.claim_due(WORKER, limit=5, lease_seconds=60)
    assert [
        request.headers["x-licencas-timestamp"] for request in recorder.requests
    ] == ["1800000000", "1800000061"]


@aio
async def test_oversized_response_is_refused() -> None:
    recorder = Recorder(
        lambda _request: httpx.Response(200, content=b" " * (lc.MAX_RESPONSE_BYTES + 1))
    )
    async with make_client(recorder) as client:
        with pytest.raises(lc.LicencasResponseError) as caught:
            await client.claim_due(WORKER, limit=5, lease_seconds=60)
    assert caught.value.code == "response_too_large"


# ---------------------------------------------------------------- sigilo do log

LOG_LINE = re.compile(
    r"licencas_http operation=[A-Za-z]+ status=(?:[0-9]{3}|none) code=[a-z][a-z0-9_]{1,63} "
    r"request_id=[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


@aio
async def test_log_has_only_operation_status_code_and_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    sent: list[httpx.Request] = []
    for scenario in SCENARIOS:
        for recorder in (
            replying(200, scenario.reply),
            replying(200, {**scenario.reply, "unexpected": TEXT}),
            replying(409, {"error": "replay_unconfirmed"}),
            replying(503, {"error": "busy"}),
            raising(httpx.ReadTimeout),
            raising(httpx.ConnectError),
            Recorder(
                lambda _r: httpx.Response(
                    302, headers={"location": "https://outro.invalid/" + PROMPT_TOKEN}
                )
            ),
        ):
            async with make_client(recorder) as client:
                try:
                    await scenario.call(client)
                except lc.LicencasError as error:
                    assert all(
                        secret not in str(error) and secret not in repr(error)
                        for secret in SENSITIVE
                    )
            sent.extend(recorder.requests)
    records = [
        record for record in caplog.records if record.name == "gateway.licencas_client"
    ]
    assert len(records) == len(SCENARIOS) * 7 == len(sent)
    for record in records:
        assert LOG_LINE.fullmatch(record.getMessage()), record.getMessage()
        assert record.exc_info is None and record.stack_info is None
    request_ids = {request.headers["x-licencas-request-id"] for request in sent}
    assert {
        record.getMessage().rsplit("request_id=", 1)[1] for record in records
    } == request_ids
    for request in sent:
        for secret in (
            request.headers["x-licencas-nonce"],
            request.headers["x-licencas-signature"],
            hashlib.sha256(request.content).hexdigest(),
        ):
            assert secret not in caplog.text
    # Nenhum dado sensível em NENHUM logger (inclusive os do httpx/httpcore).
    for secret in SENSITIVE:
        assert secret not in caplog.text
    # O endpoint não aparece no log deste módulo. A linha INFO do próprio httpx traz a URL
    # (sem cabeçalho nem corpo); o gateway mantém esse logger em WARNING (hermes_logging).
    assert all(ENDPOINT_HOST not in record.getMessage() for record in records)
    assert any(
        ENDPOINT_HOST in record.getMessage()
        for record in caplog.records
        if record.name == "httpx"
    )


@aio
async def test_local_refusal_log_has_no_request_id_or_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with make_client(replying(200, {})) as client:
        with pytest.raises(lc.LicencasRequestError) as caught:
            await client.ingest_envelope(actor(actor_jid="5511@g.us"), ENVELOPE)
    assert caught.value.request_id is None
    (record,) = [
        record for record in caplog.records if record.name == "gateway.licencas_client"
    ]
    assert (
        record.getMessage()
        == "licencas_http operation=ingestEnvelope status=none code=invalid_input request_id=none"
    )
    assert "5511@g.us" not in caplog.text and "5511@g.us" not in str(caught.value)


def test_sensitive_fields_are_hidden_from_repr() -> None:
    assert TEXT not in repr(ENVELOPE)
    intent = BY_NAME["prepareIntent"].result
    assert PROMPT_TEXT not in repr(intent) and PROMPT_TOKEN not in repr(intent)
    assert TEXT not in repr(BY_NAME["listCaptured"].result)
    assert REPLY_TOKEN not in repr(APPLY)


# ---------------------------------------------------------------- import sem efeito colateral


def test_import_has_no_side_effects() -> None:
    repo = Path(__file__).resolve().parents[2]
    probe = (
        "import sys, logging\n"
        # O pacote gateway já importa config/session/delivery no __init__ existente (fora desta
        # unidade); o que se mede aqui é o que ESTE módulo acrescenta.
        "import gateway\n"
        "before = set(sys.modules)\n"
        "import gateway.licencas_client as m\n"
        "loaded = set(sys.modules) - before\n"
        "banned = ('run_agent', 'model_tools', 'gateway.run', 'gateway.session', 'gateway.config', 'cron', 'hermes_state', 'agent')\n"
        "bad = sorted(n for n in loaded if n in banned or n.startswith(('cron.', 'agent.', 'tools.')))\n"
        "assert not bad, bad\n"
        "assert not logging.getLogger().handlers and not logging.getLogger(m.__name__).handlers\n"
        "assert sorted(m.OPERATIONS) == ['claimDue', 'confirmReceipt', 'ingestEnvelope', 'listCaptured', 'prepareIntent', 'queryFollowups', 'recordReply']\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(repo),
        "HOME": "/nonexistent",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    done = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_module_source_has_no_literal_endpoint_or_file_access() -> None:
    source = Path(lc.__file__).read_text(encoding="utf-8")
    assert "https://" not in source and "supabase" not in source.lower()
    for forbidden in (
        "print(",
        "open(",
        "WORKSPACE",
        "MEMORY",
        "read_text",
        "dotenv",
        "subprocess",
    ):
        assert forbidden not in source, forbidden
