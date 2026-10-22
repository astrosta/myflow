from dataclasses import dataclass
from enum import StrEnum

MESSAGE_CONTENT_KIND_KEY = "myflow_content_kind"
MESSAGE_PRODUCER_KIND_KEY = "myflow_producer_kind"
MESSAGE_PRODUCER_ENTITY_ID_KEY = "myflow_producer_entity_id"
PROVENANCE_KEYS: frozenset[str] = frozenset({
    MESSAGE_CONTENT_KIND_KEY,
    MESSAGE_PRODUCER_KIND_KEY,
    MESSAGE_PRODUCER_ENTITY_ID_KEY,
})


class ContentKind(StrEnum):
    MIDDLEWARE_INJECTION = "middleware_injection"
    MEMORY = "memory"
    DURABLE_CONTEXT = "durable_context"
    SKILL_BODY = "skill_body"
    IMAGE_PAYLOAD = "image_payload"


@dataclass(frozen=True, slots=True)
class MessageProvenance:
    content_kind: str
    producer_kind: str
    producer_entity_id: str | None = None


def provenance_kwargs(
    content_kind: str,
    producer_kind: str,
    *,
    producer_entity_id: str | None = None,
) -> dict[str, str]:
    kwargs = {
        MESSAGE_CONTENT_KIND_KEY: str(content_kind),
        MESSAGE_PRODUCER_KIND_KEY: str(producer_kind),
    }
    if producer_entity_id is not None:
        kwargs[MESSAGE_PRODUCER_ENTITY_ID_KEY] = str(producer_entity_id)
    return kwargs


def read_provenance(message: object) -> MessageProvenance | None:
    kwargs = getattr(message, "additional_kwargs", None)
    if not isinstance(kwargs, dict):
        return None
    content_kind = kwargs.get(MESSAGE_CONTENT_KIND_KEY)
    producer_kind = kwargs.get(MESSAGE_PRODUCER_KIND_KEY)
    if not isinstance(content_kind, str) or not isinstance(producer_kind, str):
        return None
    entity_id = kwargs.get(MESSAGE_PRODUCER_ENTITY_ID_KEY)
    return MessageProvenance(
        content_kind=content_kind,
        producer_kind=producer_kind,
        producer_entity_id=entity_id if isinstance(entity_id, str) else None,
    )
