import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bridge as module
from bridge import Bridge, Output


async def test_mute_volume_unmute_and_reconnect_keep_the_requested_level(monkeypatch):
    reader = asyncio.StreamReader()
    process = SimpleNamespace(
        stdout=reader,
        returncode=None,
        terminate=lambda: None,
        wait=AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    sink = {
        "name": "test",
        "index": 123,
        "properties": {"object.id": "10"},
        "volume": {"left": {"value": 65536}},
        "mute": False,
    }

    async def pactl(command, name, value):
        if command == "set-sink-volume":
            sink["volume"]["left"]["value"] = round(int(value[:-1]) * 65536 / 100)
        elif command == "set-sink-mute":
            sink["mute"] = value == "1"

    monkeypatch.setattr(module, "pactl", pactl)
    monkeypatch.setattr(module, "run", AsyncMock())
    monkeypatch.setattr(
        module, "pulse_list", AsyncMock(side_effect=lambda kind: [sink])
    )
    calls = []
    role = SimpleNamespace(
        get_player_volume=lambda: 30,
        get_player_muted=lambda: False,
        set_player_volume=lambda value: calls.append(("volume", value)),
        set_player_mute=lambda value: calls.append(("mute", value)),
    )
    output = Output("client", "test", "1")
    bridge = Bridge(None)

    async def settle():
        # The subscription blocks only after the current snapshot has been handled.
        for _ in range(20):
            await asyncio.sleep(0)

    async def change(volume, muted):
        calls.clear()
        sink["volume"]["left"]["value"] = round(volume * 65536 / 100)
        sink["mute"] = muted
        reader.feed_data(b"Event 'change' on sink #123\n")
        await settle()

    ready = asyncio.Event()
    task = asyncio.create_task(bridge.control_volume(output, role, ready))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        await settle()
        await change(30, True)
        assert calls == [("mute", True)]
        await change(85, True)
        assert calls == [("volume", 85)]
        await change(85, False)
        assert calls == [("mute", False), ("volume", 85)]
        await change(90, False)
        assert calls == [("volume", 90)]
        await change(80, False)
        assert calls == [("volume", 80)]
        await change(80, False)
        assert calls == []
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # A stale state from a reconnect must not overwrite the user's last selection.
    calls.clear()
    ready = asyncio.Event()
    task = asyncio.create_task(bridge.control_volume(output, role, ready))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        await settle()
        assert calls == [("mute", False), ("volume", 80)]
        assert round(sink["volume"]["left"]["value"] * 100 / 65536) == 80
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
