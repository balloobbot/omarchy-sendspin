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

import settings

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
    volume_initialized: bool = False
    members: tuple = ()
    label: str = ""


class GroupVolume:
    def __init__(self, clients):
        self.roles = [client.role("player@v1") for client in clients]
        self.muted = False

    def get_player_volume(self):
        values = [role.get_player_volume() for role in self.roles]
        return None if None in values else round(sum(values) / len(values))

    def get_player_muted(self):
        return all(
            role.get_player_muted()
            if role.get_player_muted() is not None
            else role.get_player_volume() == 0
            for role in self.roles
        )

    def set_player_volume(self, volume):
        for role in self.roles:
            role.set_player_volume(
                0 if self.muted and role.get_player_muted() is None else volume
            )

    def set_player_mute(self, muted):
        self.muted = muted
        for role in self.roles:
            if role.get_player_muted() is not None:
                role.set_player_mute(muted)
            elif muted:
                role.set_player_volume(0)


class Bridge:
    def __init__(self, server, manage_settings=False):
        self.server = server
        self.outputs = {}
        self.fallback = None
        self.manage_settings = manage_settings
        self.settings = {"players": {}, "groups": []}
        self.catalog = (
            settings.read_json(settings.catalog_path(), []) if manage_settings else []
        )

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
        if self.manage_settings:
            try:
                self.settings = settings.load()
            except (ValueError, TypeError, OSError):
                LOG.exception("Cannot read settings; keeping the last valid settings")
            known = {p["id"]: {**p, "available": False} for p in self.catalog}
            for client_id, client in clients.items():
                known[client_id] = {
                    "id": client_id,
                    "name": client.name,
                    "available": True,
                }
            catalog = sorted(known.values(), key=lambda p: (p["name"], p["id"]))
            if catalog != self.catalog:
                settings.write_json(settings.catalog_path(), catalog)
                self.catalog = catalog
        desired = {}
        for client_id, client in list(clients.items())[:32]:
            preference = self.settings["players"].get(client_id, {})
            if not preference.get("hidden"):
                desired[client_id] = (
                    (client_id,),
                    preference.get("alias") or client.name,
                )
        for group in self.settings["groups"]:
            if all(member in clients for member in group["members"]):
                desired["group:" + group["id"]] = (
                    tuple(group["members"]),
                    group["name"],
                )
        desired = dict(list(desired.items())[:64])
        for client_id in list(self.outputs):
            output = self.outputs[client_id]
            if desired.get(client_id) != (output.members, output.label):
                await self.remove(self.outputs.pop(client_id))
        for client_id, (members, label) in desired.items():
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
                f"""sink_properties="device.description='{description(label)}' """
                'device.class=sound node.virtual=true priority.session=-1000"',
            )
            self.outputs[client_id] = Output(
                client_id, name, module, members=members, label=label
            )
            LOG.info("Available: %s", description(label))
        sinks = {s["name"]: s["index"] for s in await pulse_list("sinks")}
        active = {s["sink"] for s in await pulse_list("sink-inputs") if not s["corked"]}
        # A device can play one stream. Prefer the selected default, then an
        # existing session, and move conflicting app streams to that output.
        outputs = sorted(
            self.outputs.values(), key=lambda o: (o.name != default, o.task is None)
        )
        claimed = {}
        wanted_outputs = set()
        for output in outputs:
            if sinks.get(output.name) not in active:
                continue
            conflict = next((claimed[m] for m in output.members if m in claimed), None)
            if conflict:
                for app in await pulse_list("sink-inputs"):
                    if app["sink"] == sinks.get(output.name):
                        with suppress(RuntimeError):
                            await pactl(
                                "move-sink-input", str(app["index"]), conflict.name
                            )
                continue
            wanted_outputs.add(output.client_id)
            for member in output.members:
                claimed[member] = output
        # Stop old sessions before starting any that could reuse their members.
        for output in outputs:
            if output.task and output.task.done():
                output.task.result()
                output.task = None
            if output.client_id not in wanted_outputs and output.task:
                output.task.cancel()
                with suppress(asyncio.CancelledError):
                    await output.task
                output.task = None
        for output in outputs:
            if output.client_id in wanted_outputs and output.task is None:
                output.task = asyncio.create_task(self.stream(output))

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
            muted = role.get_player_muted()
            # Adopt the device level once; subsequent sessions retain the user's selection.
            if not output.volume_initialized:
                await pactl(
                    "set-sink-volume", output.name, f"{role.get_player_volume()}%"
                )
                if muted is not None:
                    await pactl("set-sink-mute", output.name, "1" if muted else "0")
                output.volume_initialized = True
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
                    if muted is None:
                        # Keep the desired level locally while emulating mute with zero.
                        effective = 0 if current[1] else volume
                        old_effective = (
                            None
                            if previous is None
                            else (0 if previous[1] else previous[0])
                        )
                        if effective != old_effective:
                            role.set_player_volume(effective)
                    else:
                        mute_changed = previous is None or current[1] != previous[1]
                        if mute_changed:
                            role.set_player_mute(current[1])
                        # Unmute can restore a device's saved level. Apply our level last.
                        if (
                            previous is None
                            or volume != previous[0]
                            or (mute_changed and not current[1])
                        ):
                            role.set_player_volume(volume)
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
        members = output.members or (output.client_id,)
        clients = []
        try:
            for member in members:
                await self.server.trust_unpaired(member)
                self.server.reclaim_client_for_playback(member, timeout_s=10)
            async with asyncio.timeout(10):
                for member in members:
                    while True:
                        client = self.server.get_client(member)
                        if (
                            client
                            and client.is_connected
                            and "player@v1" in client.active_role_ids
                            and not client.awaits_role_state("player")
                        ):
                            clients.append(client)
                            break
                        await asyncio.sleep(0.1)
            client = clients[0]
            await client.ungroup()
            group = client.group
            for other in clients[1:]:
                await group.add_client(other)
            if len(clients) > 1:
                group.set_group_name(output.label)
            role = (
                client.role("player@v1") if len(clients) == 1 else GroupVolume(clients)
            )
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
            while all(
                c.is_connected and "player@v1" in c.active_role_ids for c in clients
            ):
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
            for client in clients:
                with suppress(Exception):
                    await client.ungroup()
            for member in members:
                await self.server.untrust_unpaired(member)

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
        bridge = Bridge(server, manage_settings=True)
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
