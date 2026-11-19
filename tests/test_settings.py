from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bridge as module
import settings
from bridge import Bridge


def test_settings_roundtrip_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    value = settings.validate(
        {
            "players": {"one": {"alias": " Living room ", "hidden": True}},
            "groups": [{"name": "Downstairs", "members": ["one", "two"]}],
        }
    )
    settings.write_json(settings.config_path(), value)
    assert settings.load() == value
    assert value["players"]["one"]["alias"] == "Living room"
    assert value["groups"][0]["id"]
    with pytest.raises(ValueError, match="different players"):
        settings.validate(
            {"groups": [{"name": "Duplicate", "members": ["one", "one"]}]}
        )
    with pytest.raises(ValueError, match="needs a name"):
        settings.validate({"groups": [{"name": " ", "members": ["one", "two"]}]})


async def test_hidden_players_can_join_groups_and_offline_groups_disappear(monkeypatch):
    def player(player_id):
        return SimpleNamespace(
            client_id=player_id,
            name=player_id,
            info_or_none=SimpleNamespace(
                unpaired_access=SimpleNamespace(enabled=True), player_support=True
            ),
        )

    server = SimpleNamespace(clients=[player("one"), player("two")])
    bridge = Bridge(server)
    bridge.settings = settings.validate(
        {
            "players": {"one": {"hidden": True}, "two": {"alias": "Office"}},
            "groups": [{"id": "both", "name": "Together", "members": ["one", "two"]}],
        }
    )
    command = AsyncMock(return_value="local")
    monkeypatch.setattr(module, "pactl", command)
    monkeypatch.setattr(module, "pulse_list", AsyncMock(return_value=[]))
    await bridge.reconcile()
    assert set(bridge.outputs) == {"two", "group:both"}
    assert bridge.outputs["two"].label == "Office"
    assert bridge.outputs["group:both"].members == ("one", "two")
    server.clients.pop()
    await bridge.reconcile()
    assert not bridge.outputs


async def test_overlapping_output_stops_before_group_starts(monkeypatch):
    import asyncio

    clients = [
        SimpleNamespace(
            client_id=p,
            name=p,
            info_or_none=SimpleNamespace(
                unpaired_access=SimpleNamespace(enabled=True), player_support=True
            ),
        )
        for p in ("one", "two")
    ]
    bridge = Bridge(SimpleNamespace(clients=clients))
    bridge.settings = settings.validate(
        {"groups": [{"id": "both", "name": "Together", "members": ["one", "two"]}]}
    )
    command = AsyncMock(return_value="local")
    monkeypatch.setattr(module, "pactl", command)
    monkeypatch.setattr(module, "pulse_list", AsyncMock(return_value=[]))
    await bridge.reconcile()
    events = []

    async def stream(output):
        events.append(("start", output.client_id))
        try:
            await asyncio.Event().wait()
        finally:
            events.append(("stop", output.client_id))

    bridge.stream = stream
    individual = bridge.outputs["one"]
    group = bridge.outputs["group:both"]
    individual.task = asyncio.create_task(stream(individual))
    await asyncio.sleep(0)
    command.return_value = group.name

    async def listing(kind):
        if kind == "sinks":
            return [
                {"name": individual.name, "index": 1},
                {"name": group.name, "index": 2},
            ]
        return [
            {"index": 10, "sink": 1, "corked": False},
            {"index": 11, "sink": 2, "corked": False},
        ]

    monkeypatch.setattr(module, "pulse_list", listing)
    await bridge.reconcile()
    await asyncio.sleep(0)
    assert events == [("start", "one"), ("stop", "one"), ("start", "group:both")]
    command.assert_any_await("move-sink-input", "10", group.name)
    group.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await group.task


def test_group_mute_support_can_differ_between_players():
    from unittest.mock import Mock

    from bridge import GroupVolume

    mute_role = SimpleNamespace(
        get_player_volume=lambda: 35,
        get_player_muted=lambda: False,
        set_player_volume=Mock(),
        set_player_mute=Mock(),
    )
    volume_role = SimpleNamespace(
        get_player_volume=lambda: 35,
        get_player_muted=lambda: None,
        set_player_volume=Mock(),
    )
    group = GroupVolume(
        [
            SimpleNamespace(role=lambda role_id: mute_role),
            SimpleNamespace(role=lambda role_id: volume_role),
        ]
    )
    assert group.get_player_volume() == 35
    assert group.get_player_muted() is False
    group.set_player_mute(True)
    group.set_player_volume(80)
    mute_role.set_player_mute.assert_called_with(True)
    mute_role.set_player_volume.assert_called_with(80)
    volume_role.set_player_volume.assert_called_with(0)
    group.set_player_mute(False)
    group.set_player_volume(80)
    mute_role.set_player_mute.assert_called_with(False)
    volume_role.set_player_volume.assert_called_with(80)
