import json
import os
import sys
from pathlib import Path
from uuid import uuid4


def config_path():
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "omarchy-sendspin/settings.json"
    )


def catalog_path():
    return (
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / "omarchy-sendspin/players.json"
    )


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate(value):
    def name(text, required=False):
        if (
            not isinstance(text, str)
            or len(text) > 128
            or any(ord(c) < 32 for c in text)
        ):
            raise ValueError("Names must be text with at most 128 characters.")
        text = text.strip()
        if required and not text:
            raise ValueError("Each group needs a name.")
        return text

    if not isinstance(value, dict):
        raise TypeError("Invalid settings.")
    players = value.get("players", {})
    groups = value.get("groups", [])
    if (
        not isinstance(players, dict)
        or len(players) > 256
        or not isinstance(groups, list)
        or len(groups) > 32
    ):
        raise ValueError("Too many players or groups.")
    result = {"players": {}, "groups": []}
    for player_id, player in players.items():
        if (
            not isinstance(player_id, str)
            or not player_id
            or len(player_id) > 256
            or not isinstance(player, dict)
        ):
            raise ValueError("Invalid player.")
        if not isinstance(player.get("hidden", False), bool):
            raise TypeError("Invalid player visibility.")
        result["players"][player_id] = {
            "alias": name(player.get("alias", "")),
            "hidden": player.get("hidden", False),
        }
    ids = set()
    for group in groups:
        if not isinstance(group, dict):
            raise TypeError("Invalid group.")
        group_id = group.get("id") or uuid4().hex
        if not isinstance(group_id, str) or len(group_id) > 64 or group_id in ids:
            raise ValueError("Invalid group identifier.")
        ids.add(group_id)
        members = group.get("members")
        if (
            not isinstance(members, list)
            or not 2 <= len(members) <= 32
            or any(
                not isinstance(member, str) or not member or len(member) > 256
                for member in members
            )
            or len(set(members)) != len(members)
        ):
            raise ValueError(
                "Choose between 2 and 32 different players for each group."
            )
        result["groups"].append(
            {
                "id": group_id,
                "name": name(group.get("name", ""), True),
                "members": members,
            }
        )
    return result


def load():
    return validate(read_json(config_path(), {}))


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["save"]:
            data = sys.stdin.readline(131073)
            if len(data) > 131072:
                raise ValueError("Settings are too large.")
            write_json(config_path(), validate(json.loads(data)))
        elif sys.argv[1:] == ["read"]:
            print(
                json.dumps(
                    {"settings": load(), "players": read_json(catalog_path(), [])}
                )
            )
        else:
            raise ValueError("Expected read or save.")
    except (ValueError, TypeError, OSError) as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
