import asyncio
import math
import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiosendspin.client import ClientListener, SendspinClient
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand, Roles
from aiosendspin.noise import (
    Identity,
    InMemoryClientPairingStore,
    InMemoryServerPairingStore,
)
from aiosendspin.server import SendspinServer

from bridge import (
    Bridge,
    description,
    eligible,
    load_identity,
    pactl,
    pulse_list,
    sink_name,
)


def test_identity_persists_with_private_permissions(tmp_path):
    identity = load_identity(tmp_path)
    assert load_identity(tmp_path).peer_id == identity.peer_id
    assert (tmp_path / "identity").stat().st_mode & 0o777 == 0o600


def test_network_names_cannot_inject_pulse_properties():
    name = """<img src="https://example.invalid"> ' sink_name=oops $(echo hi)"""
    label = description(name)
    assert all(char not in label for char in "<>\"'=$/")
    assert len(sink_name(name)) == len(sink_name("another"))
    assert sink_name(name) != sink_name("another")


def test_pairing_only_and_non_players_are_excluded():
    def client(enabled, player):
        return SimpleNamespace(
            info_or_none=SimpleNamespace(
                unpaired_access=SimpleNamespace(enabled=enabled), player_support=player
            )
        )

    assert eligible(client(True, object()))
    assert not eligible(client(False, object()))
    assert not eligible(client(True, None))
    assert not eligible(SimpleNamespace(info_or_none=None))


@pytest.mark.skipif(
    not __import__("os").environ.get("SENDSPIN_AUDIO_TEST"),
    reason="Requires the local PipeWire server",
)
@pytest.mark.parametrize("server_initiated", [False, True])
@pytest.mark.parametrize("remote_volume", [False, True, "volume-only"])
async def test_pipewire_audio_reaches_unpaired_client_and_cleans_up(
    server_initiated, remote_volume
):
    server = SendspinServer(
        loop=asyncio.get_running_loop(),
        identity=Identity.generate(),
        server_name="Omarchy test",
        pairing_store=InMemoryServerPairingStore(),
    )
    store = InMemoryClientPairingStore()
    await store.store_pairing_config(
        replace(await store.get_pairing_config(), unpaired_access_enabled=True)
    )
    client = SendspinClient(
        identity=Identity.generate(),
        client_name="Omarchy test speaker",
        roles=[Roles.PLAYER],
        pairing_store=store,
        initial_volume=35 if remote_volume else 100,
        state_supported_commands=[PlayerCommand.VOLUME]
        + ([PlayerCommand.MUTE] if remote_volume is True else [])
        if remote_volume
        else [],
        min_buffer_ms=50,
        required_lead_time_ms=1000,
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM, channels=2, sample_rate=48000, bit_depth=16
                )
            ],
            buffer_capacity=1000000,
        ),
    )
    bridge = Bridge(server)
    received = []
    send_ahead = []

    def receive(timestamp, pcm, fmt, ahead):
        received.append(pcm)
        send_ahead.append(ahead)

    client.add_audio_chunk_listener(receive)
    commands = []
    client.add_server_command_listener(lambda payload: commands.append(payload.player))
    playback = None
    listener = None
    name = sink_name(client.identity.peer_id)
    original_default = await pactl("get-default-sink")
    try:
        await server.start_server(
            port=0, host="127.0.0.1", advertise_addresses=[], discover_clients=False
        )
        port = server._app_runner.addresses[0][1]
        if server_initiated:

            async def handle(ws):
                ended = asyncio.Event()
                remove = client.add_disconnect_listener(ended.set)
                try:
                    await client.attach_websocket(ws)
                    await ended.wait()
                finally:
                    remove()

            listener = ClientListener(
                client_id=client.identity.peer_id,
                on_connection=handle,
                host="127.0.0.1",
                port=0,
                advertise_mdns=False,
            )
            await listener.start()
            client_port = listener._runner.addresses[0][1]
            await server.connect_to_client_and_wait(
                f"ws://127.0.0.1:{client_port}/sendspin"
            )
        else:
            await client.connect(f"ws://127.0.0.1:{port}/sendspin")
        await bridge.reconcile()
        sinks = await pulse_list("sinks")
        assert (
            next(s for s in sinks if s["name"] == name)["description"]
            == "Omarchy test speaker (Sendspin)"
        )
        assert await pactl("get-default-sink") == original_default
        assert not received
        assert not await server.pairing_store.trusted_unpaired(client.identity.peer_id)
        playback = await asyncio.create_subprocess_exec(
            "pacat",
            "--playback",
            "--client-name=Omarchy Sendspin test",
            "--property=node.dont-move=true",
            "--volume=65536",
            "--raw",
            f"--device={name}",
            "--rate=48000",
            "--format=s16le",
            "--channels=2",
            stdin=asyncio.subprocess.PIPE,
        )
        pcm = b"".join(
            struct.pack(
                "<hh", *([int(5000 * math.sin(i * 2 * math.pi * 440 / 48000))] * 2)
            )
            for i in range(48000)
        )
        async with asyncio.timeout(15):
            while not any(any(chunk) for chunk in received):
                playback.stdin.write(pcm[:3840])
                await playback.stdin.drain()
                await bridge.reconcile()
                await asyncio.sleep(0.02)
        assert await server.pairing_store.trusted_unpaired(client.identity.peer_id)
        # A live source honors the 50 ms minimum, not the 1000 ms buffered lead.
        assert 0 < send_ahead[0] < 250_000

        async def feed():
            for _ in range(40):
                playback.stdin.write(pcm[:3840])
                await playback.stdin.drain()
                await asyncio.sleep(0.02)

        def peak():
            return max(
                abs(sample[0])
                for chunk in received[-10:]
                for sample in struct.iter_unpack("<h", chunk)
            )

        await feed()
        if remote_volume:
            assert any(c.volume == 35 for c in commands)
        full_volume = peak()
        await pactl("set-sink-volume", name, "50%")
        await feed()
        if remote_volume:
            assert any(c.volume == 50 for c in commands)
            assert peak() == pytest.approx(full_volume, rel=0.05)
        else:
            assert 0 < peak() < full_volume * 0.6
        # Muting the selected output must silence the network stream too.
        await pactl("set-sink-mute", name, "1")
        for _ in range(80):
            playback.stdin.write(pcm[:3840])
            await playback.stdin.drain()
            await asyncio.sleep(0.02)
        if remote_volume:
            if remote_volume is True:
                assert any(c.mute is True for c in commands)
            else:
                assert any(c.volume == 0 for c in commands)
            assert peak() == pytest.approx(full_volume, rel=0.05)
        else:
            assert received[-10:] and not any(any(chunk) for chunk in received[-10:])
    finally:
        if playback and playback.returncode is None:
            playback.terminate()
            await playback.wait()
        await bridge.close()
        await client.disconnect()
        await server.close()
        if listener:
            await listener.stop()
    assert name not in {s["name"] for s in await pulse_list("sinks")}
    assert await pactl("get-default-sink") == original_default


async def test_stale_cleanup_uses_the_sink_owner_module(monkeypatch):
    from unittest.mock import AsyncMock

    import bridge as module

    output_name = sink_name("stale")
    monkeypatch.setattr(
        module,
        "pulse_list",
        AsyncMock(
            return_value=[
                {"name": "local", "owner_module": 99},
                {"name": output_name, "owner_module": 123},
            ]
        ),
    )
    command = AsyncMock()
    monkeypatch.setattr(module, "pactl", command)
    bridge = Bridge(None)
    bridge.restore = AsyncMock()
    await bridge.clean_stale()
    bridge.restore.assert_awaited_once_with(output_name)
    command.assert_awaited_once_with("unload-module", "123")


async def test_playback_failure_restores_local_audio_and_revokes_approval():
    from unittest.mock import AsyncMock, Mock

    from bridge import Output

    client = SimpleNamespace(
        is_connected=True,
        role=lambda role_id: None,
        awaits_role_state=lambda family: False,
        active_role_ids=["player@v1"],
        group=SimpleNamespace(
            start_stream=Mock(side_effect=RuntimeError("disconnected"))
        ),
    )
    server = SimpleNamespace(
        trust_unpaired=AsyncMock(),
        untrust_unpaired=AsyncMock(),
        reclaim_client_for_playback=Mock(),
        get_client=Mock(return_value=client),
    )
    bridge = Bridge(server)
    bridge.restore = AsyncMock()
    output = Output("identity", sink_name("identity"), "123")
    await bridge.stream(output)
    bridge.restore.assert_awaited_once_with(output.name)
    server.untrust_unpaired.assert_awaited_once_with("identity")
