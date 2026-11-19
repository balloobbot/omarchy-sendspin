import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import socket
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from aiosendspin.noise import Identity, InMemoryServerPairingStore
from aiosendspin.server import SendspinServer
from aiosendspin.server.audio import AudioFormat

LOG = logging.getLogger("omarchy.sendspin")
PREFIX = "omarchy_sendspin_"
FORMAT = AudioFormat(sample_rate=48000, bit_depth=16, channels=2)
CAPTURE_MS = 10
CHUNK_BYTES = 48000 * CAPTURE_MS // 1000 * 4


async def run(*args):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(5):
            out, err = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(err.decode(errors="replace").strip())
    return out.decode().strip()


async def pactl(*args):
    return await run("pactl", *args)


async def pulse_list(kind):
    return json.loads(await pactl("--format=json", "list", kind))


def sink_name(client_id):
    return PREFIX + hashlib.sha256(client_id.encode()).hexdigest()[:24]


def description(name):
    # PulseAudio parses property assignments separately from process arguments.
    safe = re.sub(r"[^\w .()-]", "", name[:128]).strip()
    return (safe or "Speaker") + " (Sendspin)"


def eligible(client):
    info = client.info_or_none
    return bool(info and info.unpaired_access.enabled and info.player_support)


def load_identity(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "identity"
    if not path.exists():
        identity = Identity.generate()
        with path.open("xb") as file:
            os.chmod(path, 0o600)
            file.write(identity.private_bytes)
        return identity
    return Identity.from_private_bytes(path.read_bytes())


@dataclass
class Output:
    client_id: str
    name: str
    module: str
    task: asyncio.Task | None = None


class Bridge:
    def __init__(self, server):
        self.server = server
        self.outputs = {}
        self.fallback = None

    async def restore(self, name):
        sinks = await pulse_list("sinks")
        local = [s for s in sinks if not s["name"].startswith(PREFIX)]
        fallback = next((s for s in local if s["name"] == self.fallback), None)
        fallback = fallback or next(iter(local), None)
        if fallback is None:
            return
        if await pactl("get-default-sink") == name:
            await pactl("set-default-sink", fallback["name"])
        indices = {s["index"] for s in sinks if s["name"] == name}
        for stream in await pulse_list("sink-inputs"):
            if stream["sink"] in indices:
                with suppress(RuntimeError):
                    await pactl(
                        "move-sink-input", str(stream["index"]), fallback["name"]
                    )

    async def remove(self, output):
        if output.task:
            output.task.cancel()
            with suppress(asyncio.CancelledError):
                await output.task
        await self.restore(output.name)
        await pactl("unload-module", output.module)

    async def clean_stale(self):
        # A forced termination can leave PulseAudio modules behind.
        for sink in await pulse_list("sinks"):
            if sink["name"].startswith(PREFIX):
                await self.restore(sink["name"])
                await pactl("unload-module", str(sink["owner_module"]))

    async def reconcile(self):
        default = await pactl("get-default-sink")
        if not default.startswith(PREFIX):
            self.fallback = default
        clients = {c.client_id: c for c in self.server.clients if eligible(c)}
        for client_id in list(self.outputs):
            if client_id not in clients:
                await self.remove(self.outputs.pop(client_id))
        for client_id, client in list(clients.items())[:32]:
            if client_id in self.outputs:
                continue
            name = sink_name(client_id)
            module = await pactl(
                "load-module",
                "module-null-sink",
                f"sink_name={name}",
                "rate=48000",
                "channels=2",
                "format=s16le",
                f"""sink_properties="device.description='{description(client.name)}' """
                'device.class=sound node.virtual=true priority.session=-1000"',
            )
            self.outputs[client_id] = Output(client_id, name, module)
            LOG.info("Available: %s", description(client.name))
        sinks = {s["name"]: s["index"] for s in await pulse_list("sinks")}
        active = {s["sink"] for s in await pulse_list("sink-inputs") if not s["corked"]}
        for output in self.outputs.values():
            wanted = sinks.get(output.name) in active
            if output.task and output.task.done():
                output.task.result()
                output.task = None
            if wanted and output.task is None:
                output.task = asyncio.create_task(self.stream(output))
            elif not wanted and output.task:
                output.task.cancel()
                with suppress(asyncio.CancelledError):
                    await output.task
                output.task = None

    async def control_volume(self, output, role, ready):
        subscriber = await asyncio.create_subprocess_exec(
            "pactl",
            "subscribe",
            stdout=asyncio.subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C"},
        )
        try:
            sink = next(
                s for s in await pulse_list("sinks") if s["name"] == output.name
            )
            # Keep the player's current loudness when switching from local gain.
            await pactl("set-sink-volume", output.name, f"{role.get_player_volume()}%")
            muted = role.get_player_muted()
            if muted is not None:
                await pactl("set-sink-mute", output.name, "1" if muted else "0")
            await run(
                "pw-cli",
                "set-param",
                str(sink["properties"]["object.id"]),
                "Props",
                '{"params": ["monitor.channel-volumes", false], "monitorVolumes": [1.0, 1.0], "monitorMute": false}',
            )
            previous = None
            ready.set()
            while True:
                sink = next(
                    s for s in await pulse_list("sinks") if s["name"] == output.name
                )
                volume = min(
                    100,
                    max(
                        0,
                        round(
                            max(v["value"] for v in sink["volume"].values())
                            * 100
                            / 65536
                        ),
                    ),
                )
                current = (volume, sink["mute"])
                if current != previous:
                    # Players without mute support use zero volume while muted.
                    role.set_player_volume(
                        0 if current[1] and muted is None else volume
                    )
                    if muted is not None:
                        role.set_player_mute(current[1])
                    previous = current
                while True:
                    line = await subscriber.stdout.readline()
                    if not line:
                        raise RuntimeError("PulseAudio volume subscription ended")
                    if line.rstrip().endswith(f"on sink #{sink['index']}".encode()):
                        break
        finally:
            if subscriber.returncode is None:
                subscriber.terminate()
            await subscriber.wait()

    async def stream(self, output):
        capture = None
        stream = None
        volume_task = None
        try:
            await self.server.trust_unpaired(output.client_id)
            self.server.reclaim_client_for_playback(output.client_id, timeout_s=10)
            async with asyncio.timeout(10):
                while True:
                    client = self.server.get_client(output.client_id)
                    if (
                        client
                        and client.is_connected
                        and "player@v1" in client.active_role_ids
                        and not client.awaits_role_state("player")
                    ):
                        break
                    await asyncio.sleep(0.1)
            role = client.role("player@v1")
            if role and role.get_player_volume() is not None:
                ready = asyncio.Event()
                volume_task = asyncio.create_task(
                    self.control_volume(output, role, ready)
                )
                async with asyncio.timeout(5):
                    while not ready.is_set():
                        if volume_task.done():
                            volume_task.result()
                        await asyncio.sleep(0.01)
            stream = client.group.start_stream()
            # Live capture cannot fill the larger queue used for buffered media.
            stream.set_live_source(True)
            capture = await asyncio.create_subprocess_exec(
                "parec",
                f"--device={output.name}.monitor",
                "--format=s16le",
                "--rate=48000",
                "--channels=2",
                f"--latency-msec={CAPTURE_MS}",
                "--client-name=Omarchy Sendspin",
                "--property=node.dont-move=true",
                "--raw",
                stdout=asyncio.subprocess.PIPE,
                limit=CHUNK_BYTES * 4,
            )
            LOG.info("Streaming to %s", description(client.name))
            while client.is_connected and "player@v1" in client.active_role_ids:
                if volume_task and volume_task.done():
                    volume_task.result()
                async with asyncio.timeout(5):
                    pcm = await capture.stdout.readexactly(CHUNK_BYTES)
                    stream.prepare_audio(pcm, FORMAT)
                    await stream.commit_audio()
            raise RuntimeError(
                "Player disconnected or playback moved to another server"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Playback failed for %s", output.name)
            await self.restore(output.name)
        finally:
            if volume_task:
                volume_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await volume_task
            if capture and capture.returncode is None:
                capture.terminate()
                try:
                    async with asyncio.timeout(2):
                        await capture.wait()
                except TimeoutError:
                    capture.kill()
                    await capture.wait()
            if stream:
                stream.stop()
            if volume_task:
                with suppress(RuntimeError, TimeoutError):
                    sink = next(
                        (
                            s
                            for s in await pulse_list("sinks")
                            if s["name"] == output.name
                        ),
                        None,
                    )
                    if sink:
                        await run(
                            "pw-cli",
                            "set-param",
                            str(sink["properties"]["object.id"]),
                            "Props",
                            '{"params": ["monitor.channel-volumes", true]}',
                        )
            await self.server.untrust_unpaired(output.client_id)

    async def close(self):
        for output in list(self.outputs.values()):
            with suppress(RuntimeError):
                await self.remove(output)
        self.outputs.clear()


async def main():
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("aiosendspin").setLevel(logging.WARNING)
    directory = (
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / "omarchy-sendspin"
    )
    identity = load_identity(directory)
    with (directory / "bridge.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        server = SendspinServer(
            loop=loop,
            identity=identity,
            server_name=f"Omarchy ({socket.gethostname()})"[:64],
            pairing_store=InMemoryServerPairingStore(),
        )
        bridge = Bridge(server)
        try:
            await bridge.clean_stale()
            await server.start_server(port=8929)
            while not stop.is_set():
                await bridge.reconcile()
                with suppress(TimeoutError):
                    async with asyncio.timeout(1):
                        await stop.wait()
        finally:
            await bridge.close()
            await server.close()


if __name__ == "__main__":
    asyncio.run(main())
