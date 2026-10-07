from typing import Any

import pytest
from jsonschema import Draft202012Validator

from canoe17_mcp.catalogue import Catalogue, load_catalogue
from canoe17_mcp.contracts import BackendError, ErrorCode


def test_packaged_catalogue_and_flat_discovery() -> None:
    catalogue = Catalogue()
    tools = catalogue.discovery()
    assert len(tools) == 22
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert not {"oneOf", "anyOf", "allOf"} & schema.keys()
        assert schema["additionalProperties"] is False
        assert "validationSchema" not in tool
        Draft202012Validator.check_schema(schema)
    for tool in load_catalogue()["tools"]:
        Draft202012Validator.check_schema(tool["validationSchema"])
    tools[0]["inputSchema"]["properties"]["injected"] = {}
    assert "injected" not in catalogue.discovery()[0]["inputSchema"]["properties"]


@pytest.mark.parametrize(
    ("name", "args", "write"),
    [
        ("canoe_status", {}, False),
        ("canoe_database", {"action": "list"}, False),
        ("canoe_database", {"action": "set_channel", "database_id": "db:A", "channel": 1}, True),
        ("canoe_open_config", {"path": "a.cfg"}, True),
        (
            "canoe_diag_request",
            {"action": "start", "network": "CAN", "ecu_qualifier": "Door", "raw_hex": "10 01"},
            True,
        ),
        ("canoe_send_can_frame", {"channel": 1, "can_id": 2047, "data_hex": "00"}, True),
        (
            "canoe_send_can_frame",
            {"channel": 1, "can_id": 2048, "extended": True, "fd": True, "data_hex": "00" * 12},
            True,
        ),
    ],
)
def test_action_validation(name: str, args: dict[str, Any], write: bool) -> None:
    validated, actual = Catalogue().validate(name, args)
    assert actual == write
    assert ("confirm" in validated) == write
    assert "confirm" not in args


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("canoe_database", {"action": "list", "confirm": True}),
        ("canoe_database", {"action": "remove"}),
        ("canoe_database", {"action": "set_channel", "database_id": "db:A", "channel": True}),
        ("canoe_status", {"unknown": True}),
        (
            "canoe_diag_request",
            {"action": "start", "network": "CAN", "ecu_qualifier": "Door", "raw_hex": "1"},
        ),
        (
            "canoe_diag_request",
            {"action": "start", "network": "CAN", "ecu_qualifier": "Door", "raw_hex": "  "},
        ),
        (
            "canoe_diag_request",
            {
                "action": "start",
                "network": "CAN",
                "ecu_qualifier": "Door",
                "raw_hex": "10",
                "service_qualifier": "Start",
            },
        ),
        ("canoe_send_can_frame", {"channel": 1, "can_id": 2048, "data_hex": ""}),
        ("canoe_send_can_frame", {"channel": 1, "can_id": 1, "data_hex": "", "brs": True}),
        ("canoe_send_can_frame", {"channel": 1, "can_id": 1, "data_hex": "00" * 9}),
        ("canoe_send_can_frame", {"channel": 1, "can_id": 1, "data_hex": "00" * 9, "fd": True}),
    ],
)
def test_invalid_calls_fail_before_dispatch(name: str, args: dict[str, Any]) -> None:
    with pytest.raises(BackendError) as caught:
        Catalogue().validate(name, args)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENT


def test_configured_timeout_applies_to_validation_and_discovery() -> None:
    catalogue = Catalogue(maximum_timeout_s=4)
    with pytest.raises(BackendError):
        catalogue.validate(
            "canoe_diag_request",
            {
                "action": "start",
                "network": "CAN",
                "ecu_qualifier": "Door",
                "raw_hex": "10 01",
                "timeout_s": 5,
            },
        )
    tool = next(t for t in catalogue.discovery() if t["name"] == "canoe_diag_request")
    assert tool["inputSchema"]["properties"]["timeout_s"]["maximum"] == 4


def test_schema_followups_are_promoted() -> None:
    tools = {t["name"]: t for t in load_catalogue()["tools"]}
    qualifier = tools["canoe_diag_description"]["inputSchema"]["properties"]["qualifier"]
    assert "diag:" in qualifier["description"] and "@n" in qualifier["description"]
    assert "tm-sim:" in tools["canoe_test_run"]["description"]
