"""Run-create resume commands and interrupt serialization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langgraph.types import Command, Interrupt

from agentkit.runtime.runs.worker import serialize, to_jsonable
from backend.app.gateway.services import (
    RunRequestInvalid,
    _persisted_kwargs,
    normalize_input,
)
from backend.app.gateway.run_models import RunCreateRequest
from backend.app.gateway.state_reader import read_thread_state


def test_resume_command_is_rebuilt_from_whitelisted_fields() -> None:
    result = normalize_input(
        None,
        command={
            "resume": {
                "decisions": [
                    {"type": "approve"},
                    {"type": "reject", "message": "不要改"},
                ]
            }
        },
        trusted=False,
    )

    assert isinstance(result, Command)
    assert result.resume == {
        "decisions": [
            {"type": "approve"},
            {"type": "reject", "message": "不要改"},
        ]
    }


@pytest.mark.parametrize(
    "command",
    [
        {"goto": "tools"},
        {"resume": {"decisions": []}},
        {"resume": {"decisions": [{"type": "edit"}]}},
        {"resume": {"decisions": [{"type": "approve", "extra": 1}]}},
        {"resume": {"decisions": [{"type": "reject", "message": 3}]}},
    ],
)
def test_resume_command_rejects_graph_control_and_unknown_decisions(command) -> None:
    with pytest.raises(RunRequestInvalid):
        normalize_input(None, command=command, trusted=False)


def test_resume_command_cannot_be_combined_with_input() -> None:
    with pytest.raises(RunRequestInvalid):
        normalize_input(
            {"messages": [{"type": "human", "content": "hi"}]},
            command={"resume": {"decisions": [{"type": "approve"}]}},
            trusted=False,
        )


def test_resume_command_is_persisted_for_idempotency() -> None:
    body = RunCreateRequest(
        assistant_id="lead_agent",
        command={"resume": {"decisions": [{"type": "approve"}]}},
    )

    assert _persisted_kwargs(body)["command"] == body.command


def test_state_route_surfaces_pending_interrupt_after_refresh() -> None:
    saved = SimpleNamespace(
        checkpoint={"channel_values": {"messages": []}},
        config={"configurable": {"checkpoint_id": "cp-1", "checkpoint_ns": ""}},
        pending_writes=[("task-1", "__interrupt__", [
            Interrupt(
                value={
                    "action_requests": [{"name": "update_user_profile", "description": "确认"}],
                    "review_configs": [{"allowed_decisions": ["approve", "reject"]}],
                },
                id="interrupt-1",
            )
        ])],
    )

    class _Checkpointer:
        async def aget_tuple(self, _config):
            return saved

    services = SimpleNamespace(
        run_context=SimpleNamespace(checkpointer=_Checkpointer()),
    )

    state = asyncio.run(read_thread_state(services, "t1"))

    assert state.values["__interrupt__"] == [
        {
            "id": "interrupt-1",
            "value": {
                "action_requests": [{"name": "update_user_profile", "description": "确认"}],
                "review_configs": [{"allowed_decisions": ["approve", "reject"]}],
            },
        }
    ]


def test_updates_frame_serializes_interrupt_for_sse() -> None:
    frame = serialize(
        {
            "__interrupt__": (
                Interrupt(
                    value={
                        "action_requests": [{"name": "update_user_profile", "description": "确认"}],
                        "review_configs": [{"allowed_decisions": ["approve"]}],
                    },
                    id="interrupt-1",
                ),
            )
        },
        mode="updates",
    )

    assert frame["__interrupt__"][0]["id"] == "interrupt-1"
    assert frame["__interrupt__"][0]["value"]["action_requests"][0]["name"] == "update_user_profile"


def test_interrupt_dataclass_is_serialized_for_sse() -> None:
    payload = to_jsonable(
        Interrupt(
            value={
                "action_requests": [
                    {"name": "update_user_profile", "args": {"fields": {}}, "description": "确认"}
                ],
                "review_configs": [
                    {"action_name": "update_user_profile", "allowed_decisions": ["approve"]}
                ],
            },
            id="interrupt-1",
        )
    )

    assert payload == {
        "value": {
            "action_requests": [
                {"name": "update_user_profile", "args": {"fields": {}}, "description": "确认"}
            ],
            "review_configs": [
                {"action_name": "update_user_profile", "allowed_decisions": ["approve"]}
            ],
        },
        "id": "interrupt-1",
    }
