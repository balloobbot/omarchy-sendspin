# Omarchy Sendspin

- `bridge.py` implements discovery, PipeWire outputs, streaming, and cleanup.
- `plugin/` contains the Omarchy service plugin.
- `install.sh` installs the runtime and user service.
- `tests/test_bridge.py` contains unit and local audio integration tests.
- `tests/test_volume_sequence.py` checks mute, volume, and reconnect ordering.
- `README.md` describes installation, behavior, and checks.

Use `uv`. Run the checks in README.md before committing. The audio integration tests receive audio locally without sending it to physical speakers. Do not edit packaged Omarchy files.
