"""Barreira determinística do grupo protegido de Licenças (A2-pregate; U1 do isolamento).

O grupo protegido não chega ao agente, ao modelo nem ao caminho legado do WhatsApp, e os
caminhos genéricos de envio não o alcançam. A única entrada dele é o inbox durável da bridge
(A1), consumido pelo conector; aqui a mensagem é só barrada e contada.

A guarda não tem segredo: não lê arquivo de credenciais, não carrega capability nem chave HMAC,
não importa o cliente da fachada e não consulta variáveis do processo. Ela só decide, a partir
de duas fontes não secretas do perfil base, quais grupos de WhatsApp ficam barrados:

* o **manifesto** ``licencas/protected-groups.json`` e o **sinal de adoção** ``licencas/adopted``,
  independentes da configuração do consumidor: depois da adoção, manifesto ausente, ilegível,
  não confiável ou malformado fecha todos os grupos e nunca solta o piloto;
* a seção ``licencas_followup`` do ``config.yaml`` (compatibilidade com o A2-pregate): ``enabled``
  pertence ao consumidor e **nunca** remove a proteção de um ``group_jid`` válido.

Nenhum dado da mensagem é lido além do identificador do chat, e nada dela vai para log.

Falha fechada: configuração ilegível mantém o grupo barrado, e uma exceção dentro da barreira
bloqueia. Limite conhecido: manifesto e marcador são arquivos do mesmo usuário do gateway; quem
os edita (inclusive root administrativo) muda a guarda no próximo reinício.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import stat
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)

logger = logging.getLogger(__name__)

#: Seção do ``config.yaml`` do gateway.
CONFIG_SECTION = "licencas_followup"
#: Erro devolvido pelos caminhos genéricos de envio ao tentar o grupo protegido.
OUTBOUND_REFUSED = "licencas_followup_protected_group"
#: Diretório, relativo ao perfil base, do manifesto e do sinal de adoção.
ADOPTION_DIR = "licencas"
MANIFEST_FILE = "protected-groups.json"
MANIFEST_VERSION = 1
ADOPTION_MARKER = "adopted"
_MARKER_CONTENT = b"licencas-guard-adopted v1\n"
_MAX_MANIFEST_BYTES = 4096
_MAX_MANIFEST_GROUPS = 8
_NOT_OWNER_ONLY = 0o022  # escrita para grupo ou outros
#: Chaves que nunca podem estar na seção do ``config.yaml``; só o nome é conferido.
_SECRET_SECTION_KEYS = frozenset({
    "capability",
    "hmac_key",
    "hmac_key_b64",
    "licencas_capability",
    "licencas_hmac_key_b64",
})

_ENABLED_KEY = "enabled"
_GROUP_KEY = "group_jid"
_GROUP_SUFFIX = "@g.us"
_DEVICE_SUFFIX = re.compile(r":[0-9]+(?=@)")
_MERGE_TAG = "tag:yaml.org,2002:merge"
_MAP_TAG = "tag:yaml.org,2002:map"
_KEY_TAGS = frozenset({"tag:yaml.org,2002:str"})
_VALUE_TAGS = frozenset({
    "tag:yaml.org,2002:str",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:null",
    "tag:yaml.org,2002:float",
})
# Mesmo formato de grupo que o cliente e o banco exigem (``licencas_client._GROUP_JID``).
_GROUP_JID = re.compile(r"[0-9]{6,32}(?:-[0-9]{6,20})?@g\.us")


class GuardState(enum.Enum):
    DISABLED = "disabled"
    PROTECTED = "protected"
    CONFIG_ERROR = "config_error"


class BlockKind(enum.Enum):
    """Tipos de bloqueio: o único dado que vai para log e contador."""

    INBOUND_BRIDGE = "inbound_bridge"
    INBOUND_EVENT = "inbound_event"
    DISPATCH = "dispatch"
    OUTBOUND = "outbound"
    BARRIER_ERROR = "barrier_error"


class ManifestError(Exception):
    """Instalação adotada cujo manifesto não pode ser usado; ``reason`` é um código, nunca valor."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _normalize(chat_id: str) -> str:
    return _DEVICE_SUFFIX.sub("", chat_id.strip().lower())


def _chat_ids(value: object) -> Iterator[str]:
    """Formas em que um identificador de chat pode chegar: texto, coleção ou outro escalar."""
    if value is None:
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _chat_ids(item)
        return
    yield _normalize(value if isinstance(value, str) else str(value))


@dataclass(frozen=True, slots=True)
class ProtectedGroupGuard:
    """Decisão trusted dos grupos protegidos, congelada para a vida do processo.

    ``groups`` são os grupos barrados; ``all_groups`` fecha todo grupo de WhatsApp quando o
    alvo não é recuperável. ``DISABLED`` não carrega alvo e os outros estados exigem um.
    """

    state: GuardState
    groups: frozenset[str] = frozenset()
    all_groups: bool = False
    config_error: str | None = None

    def __post_init__(self) -> None:
        has_target = bool(self.groups) or self.all_groups
        if has_target is (self.state is GuardState.DISABLED):
            raise ValueError("guard state and protection target disagree")

    def protects(self, chat_id: object) -> bool:
        for candidate in _chat_ids(chat_id):
            if candidate in self.groups:
                return True
            if self.all_groups and candidate.endswith(_GROUP_SUFFIX):
                return True
        return False


def _closed(reason: str, groups: frozenset[str] = frozenset()) -> ProtectedGroupGuard:
    """Estado de erro que protege ``groups``, ou todos os grupos se não há alvo."""
    return ProtectedGroupGuard(
        state=GuardState.CONFIG_ERROR,
        groups=groups,
        all_groups=not groups,
        config_error=reason,
    )


def _identifiable_group(section: object) -> str | None:
    """JID de grupo da seção, só se for um JID de grupo de WhatsApp bem formado."""
    if not isinstance(section, Mapping):
        return None
    value = section.get(_GROUP_KEY)
    if not isinstance(value, str):
        return None
    candidate = _normalize(value)
    return candidate if _GROUP_JID.fullmatch(candidate) else None


def _declares_no_group(section: Mapping[object, object]) -> bool:
    value = section.get(_GROUP_KEY)
    return value is None or (isinstance(value, str) and value.strip() == "")


def _config_decision(raw_config: object) -> tuple[frozenset[str], str | None, bool]:
    """O que o ``config.yaml`` sozinho manda: ``(grupos, erro, declarou_alvo)``.

    ``declarou_alvo`` é falso só quando não há seção, ou há uma seção desligada sem grupo (os
    defaults): nesse caso a instalação segue sem integração. ``enabled`` não decide nada além
    de validar o próprio tipo.
    """
    if not isinstance(raw_config, Mapping):
        return frozenset(), "config_root", True
    if CONFIG_SECTION not in raw_config:
        return frozenset(), None, False
    section = raw_config[CONFIG_SECTION]
    group = _identifiable_group(section)
    groups = frozenset({group}) if group else frozenset()
    if not isinstance(section, Mapping):
        return groups, CONFIG_SECTION, True
    if any(
        isinstance(key, str) and key.lower() in _SECRET_SECTION_KEYS for key in section
    ):
        return groups, "secret_in_config", True
    enabled = section.get(_ENABLED_KEY)
    if enabled is not True and enabled is not False:
        return groups, _ENABLED_KEY, True
    if enabled is False and _declares_no_group(section):
        return groups, None, False
    return groups, None if group else _GROUP_KEY, True


def _build_guard(
    raw_config: object, read_manifest: Callable[[], frozenset[str] | None]
) -> ProtectedGroupGuard:
    try:
        manifest = read_manifest()
    except ManifestError as problem:
        return _closed(problem.reason)
    groups, error, declared = _config_decision(raw_config)
    if manifest is not None:
        # Instalação adotada: o manifesto manda; config ruim só soma erro e nunca amplia nem solta.
        groups |= manifest
    elif not declared:
        return ProtectedGroupGuard(state=GuardState.DISABLED)
    if error is not None:
        return _closed(error, groups)
    return ProtectedGroupGuard(state=GuardState.PROTECTED, groups=groups)


def load_guard(
    raw_config: object,
    read_manifest: Callable[[], frozenset[str] | None] | None = None,
) -> ProtectedGroupGuard:
    """Monta o guarda a partir do ``config.yaml`` já lido e do manifesto. Nunca levanta.

    ``read_manifest`` devolve os grupos do manifesto, ``None`` se a instalação não foi adotada,
    ou levanta ``ManifestError`` se foi e o manifesto não serve (padrão: arquivos do perfil base).
    """
    try:
        guard = _build_guard(raw_config, read_manifest or _read_manifest)
    except Exception as error:  # falha fechada: erro ao montar o guarda bloqueia
        logger.error(
            "licencas_followup guard load failed error=%s", type(error).__name__
        )
        guard = _closed("guard_load_error")
    report = logger.error if guard.state is GuardState.CONFIG_ERROR else logger.info
    report(
        "licencas_followup guard state=%s scope=%s reason=%s",
        guard.state.value,
        "all_whatsapp_groups"
        if guard.all_groups
        else ("group" if guard.groups else "none"),
        guard.config_error or "none",
    )
    return guard


class AmbiguousConfigError(yaml.YAMLError):
    """A seção efetiva não é exatamente a seção escrita: há mais de uma leitura possível."""


class _RecordingLoader(yaml.SafeLoader):
    """``SafeLoader`` que guarda quais nós receberam âncora (o padrão as descarta)."""

    anchored: frozenset[int] = frozenset()

    def compose_document(self) -> yaml.Node:
        self.get_event()
        node = self.compose_node(None, None)
        self.get_event()
        self.anchored = frozenset(id(anchored) for anchored in self.anchors.values())
        self.anchors = {}
        return node


def _explicit_section(loader: _RecordingLoader, root: yaml.Node) -> dict[str, object]:
    """A seção como está escrita: um único mapa direto sob a raiz, só de escalares simples.

    Recusa tudo o que faria o parser normal devolver algo que não está escrito ali: seção
    repetida ou trazida por merge, merge na raiz, âncora ou alias na seção, tag que mude o
    tipo, chave repetida ou não escalar, valor que não seja escalar.
    """

    def plain(node: yaml.Node, tags: frozenset[str]) -> bool:
        return (
            isinstance(node, yaml.ScalarNode)
            and id(node) not in loader.anchored
            and node.tag in tags
            # Tag explícita só passa se for a que o texto já teria sozinho.
            and node.tag
            == loader.resolve(
                yaml.ScalarNode,
                node.value,
                (node.style is None, node.style is not None),
            )
        )

    if not isinstance(root, yaml.MappingNode):
        raise AmbiguousConfigError("root")
    sections = []
    for key, value in root.value:
        if isinstance(key, yaml.ScalarNode) and key.tag == _MERGE_TAG:
            raise AmbiguousConfigError("merge at root")
        if isinstance(key, yaml.ScalarNode) and key.value == CONFIG_SECTION:
            sections.append((key, value))
    if len(sections) != 1:
        raise AmbiguousConfigError("section count")
    key, section = sections[0]
    if (
        not plain(key, _KEY_TAGS)
        or not isinstance(section, yaml.MappingNode)
        or section.tag != _MAP_TAG
        or id(section) in loader.anchored
    ):
        raise AmbiguousConfigError("section node")
    explicit: dict[str, object] = {}
    for name, value in section.value:
        if (
            not plain(name, _KEY_TAGS)
            or not plain(value, _VALUE_TAGS)
            or name.value in explicit
        ):
            raise AmbiguousConfigError("section entry")
        explicit[name.value] = loader.construct_object(value)
    return explicit


def _typed(section: object) -> object:
    """Forma comparável que não confunde ``True`` com ``1`` nem ``1`` com ``1.0``."""
    if not isinstance(section, dict):
        return None
    return {key: (type(value), value) for key, value in section.items()}


def _load_config_text(text: str) -> object:
    """O que ``yaml.safe_load`` devolve, desde que a seção efetiva seja a explícita.

    Sem a chave da seção no resultado, a integração está ausente e qualquer YAML que o resto
    do Hermes aceite serve. Com ela, o valor efetivo (depois de merges, aliases e repetições)
    tem de ser idêntico ao único mapa escrito diretamente sob a raiz; senão a configuração
    tem mais de uma leitura e é recusada.
    """
    # Arquivo vazio (sem documento) é "sem integração"; raiz ``null`` explícita não é mapa.
    if yaml.compose(text, Loader=yaml.SafeLoader) is None:
        return {}
    effective = yaml.safe_load(text)
    if not isinstance(effective, Mapping) or CONFIG_SECTION not in effective:
        return effective
    # Segunda composição, nunca construída: construir achata os merges dentro dos nós e
    # apagaria justamente o que precisa ser visto.
    loader = _RecordingLoader(text)
    try:
        explicit = _explicit_section(loader, loader.get_single_node())
    finally:
        loader.dispose()
    if _typed(effective[CONFIG_SECTION]) != _typed(explicit):
        raise AmbiguousConfigError("effective section differs from the explicit one")
    return effective


def _base_home() -> Path:
    """HERMES_HOME do perfil base do processo, ignorando override de perfil por contexto.

    A guarda protege o grupo que este gateway atende pela bridge dele; um job de cron
    rodando sob outro perfil não pode trocar a configuração que a decide.
    """
    token = set_hermes_home_override(None)
    try:
        return get_hermes_home()
    finally:
        reset_hermes_home_override(token)


def _read_config_file() -> object:
    """``config.yaml`` do perfil base, sem tolerar erro de leitura (arquivo ausente = sem seção)."""
    try:
        text = (_base_home() / "config.yaml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return _load_config_text(text)


def _read_trusted_file(directory: Path, name: str, limit: int) -> bytes | None:
    """Conteúdo (no máximo ``limit + 1`` bytes) de um arquivo confiável, ou ``None`` se não existe.

    Confiável: aberto sem seguir link, regular, do usuário do processo, sem link físico e sem
    escrita para grupo e outros, tudo conferido no descritor aberto.
    """
    try:
        descriptor = os.open(
            directory / name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise ManifestError("adoption_file") from None
    try:
        info = os.fstat(descriptor)
        if not (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and not info.st_mode & _NOT_OWNER_ONLY
        ):
            raise ManifestError("adoption_file")
        return os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _parse_manifest(content: bytes) -> frozenset[str]:
    """Manifesto estrito: ``{"version": 1, "groups": [<1 a 8 JIDs de grupo canônicos>]}``."""
    try:
        if len(content) > _MAX_MANIFEST_BYTES:
            raise ValueError("too large")
        document = json.loads(
            content.decode("utf-8"), object_pairs_hook=_no_duplicate_keys
        )
    except (ValueError, RecursionError):
        raise ManifestError("manifest_format") from None
    if not isinstance(document, dict) or set(document) != {"version", "groups"}:
        raise ManifestError("manifest_format")
    version, groups = document["version"], document["groups"]
    if (
        type(version) is not int
        or version != MANIFEST_VERSION
        or not isinstance(groups, list)
        or not 1 <= len(groups) <= _MAX_MANIFEST_GROUPS
        or not all(isinstance(g, str) and _GROUP_JID.fullmatch(g) for g in groups)
        or len(set(groups)) != len(groups)
    ):
        raise ManifestError("manifest_format")
    return frozenset(groups)


def _read_manifest() -> frozenset[str] | None:
    """Grupos do manifesto do perfil base; ``None`` se a instalação não foi adotada.

    Adotada = existe o marcador ``adopted`` **ou** o manifesto. Depois da adoção, qualquer
    falha (manifesto ausente, não confiável, malformado, marcador inválido, diretório ilegível)
    levanta ``ManifestError``: ausente nunca libera. Em plataforma sem ``O_NOFOLLOW`` ou
    ``geteuid`` uma instalação com o diretório falha fechada (todos os grupos).
    """
    directory = _base_home() / ADOPTION_DIR
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return None
    except OSError:
        raise ManifestError("adoption_state") from None
    if not (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and not info.st_mode & _NOT_OWNER_ONLY
    ):
        raise ManifestError("adoption_dir")
    marker = _read_trusted_file(directory, ADOPTION_MARKER, len(_MARKER_CONTENT))
    manifest = _read_trusted_file(directory, MANIFEST_FILE, _MAX_MANIFEST_BYTES)
    if marker is None and manifest is None:
        return None
    if marker is not None and marker != _MARKER_CONTENT:
        raise ManifestError("adoption_marker")
    if manifest is None:
        raise ManifestError("manifest_missing")
    return _parse_manifest(manifest)


_guard: ProtectedGroupGuard | None = None
_guard_lock = threading.Lock()
_block_counts: Counter[str] = Counter()
_count_lock = threading.Lock()


def current_guard() -> ProtectedGroupGuard:
    """Guarda do processo, lido uma vez; mudar a configuração exige reiniciar o gateway."""
    global _guard
    if _guard is None:
        with _guard_lock:
            if _guard is None:
                try:
                    raw_config = _read_config_file()
                # Qualquer erro de leitura ou do parser (inclusive ``RecursionError``)
                # é configuração ilegível: o gateway sobe e a guarda fica fechada.
                except Exception as error:
                    logger.error(
                        "licencas_followup config unreadable error=%s",
                        type(error).__name__,
                    )
                    raw_config = None
                _guard = load_guard(raw_config)
    return _guard


def block_counts() -> dict[str, int]:
    """Contadores técnicos por tipo de bloqueio."""
    with _count_lock:
        return dict(_block_counts)


def _record(kind: BlockKind) -> None:
    with _count_lock:
        _block_counts[kind.value] += 1
        count = _block_counts[kind.value]
    logger.warning("licencas_followup blocked kind=%s count=%d", kind.value, count)


def _decide(kind: BlockKind, claims: Callable[[ProtectedGroupGuard], bool]) -> bool:
    try:
        blocked = claims(current_guard())
    except Exception as error:  # falha fechada: erro na barreira bloqueia
        logger.error(
            "licencas_followup barrier error=%s kind=%s",
            type(error).__name__,
            kind.value,
        )
        _record(BlockKind.BARRIER_ERROR)
        return True
    if blocked:
        _record(kind)
    return blocked


def blocks_bridge_message(data: object) -> bool:
    """Mensagem crua da bridge legada: ``True`` = não construir evento nem tocar mídia."""

    def claims(guard: ProtectedGroupGuard) -> bool:
        if not isinstance(data, Mapping):
            raise TypeError("bridge message")
        return guard.protects(data.get("chatId"))

    return _decide(BlockKind.INBOUND_BRIDGE, claims)


def blocks_event(event: object, kind: BlockKind) -> bool:
    """Evento já montado, de qualquer plataforma ou origem, que diga ser do grupo protegido."""

    def claims(guard: ProtectedGroupGuard) -> bool:
        source = getattr(event, "source", None)
        raw_message = getattr(event, "raw_message", None)
        return guard.protects((
            getattr(source, "chat_id", None),
            getattr(source, "chat_id_alt", None),
            getattr(source, "parent_chat_id", None),
            raw_message.get("chatId") if isinstance(raw_message, Mapping) else None,
        ))

    return _decide(kind, claims)


def blocks_outbound(chat_id: object) -> bool:
    """Envio genérico: o grupo protegido só recebe pela rota durável com ``attempt_id`` (A1)."""
    return _decide(BlockKind.OUTBOUND, lambda guard: guard.protects(chat_id))
