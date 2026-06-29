"""Per-user configuration for the local manual scoring tool.

The config file lives under the user config dir (``$XDG_CONFIG_HOME`` or
``~/.config``) at ``local-manual-scoring-tool/config.json``, so it works whether
the tool is run from a checkout or installed with ``uv tool install``.

Persisted mutations also append a human-readable entry to ``changed``. The UI
drains that list on the first scoring page render of a session and shows it as a
banner so the user sees what their config now contains.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

_CONFIG_FILENAME = "config.json"
_APP_DIR_NAME = "local-manual-scoring-tool"

ClearOnSubmit = Literal["all", "none", "scores", "text"]
CLEAR_ON_SUBMIT_VALUES: tuple[ClearOnSubmit, ...] = ("all", "none", "scores", "text")

# Config key selecting how the scoring form is cleared after a successful submit.
CLEAR_ON_SUBMIT_KEY = "clear_on_submit"
CLEAR_ON_SUBMIT_DEFAULT: ClearOnSubmit = "scores"

# KeyboardEvent.key for the hotkey that toggles the scoring timer. Empty string
# disables. Default is the Pause/Break key.
PAUSE_TIMER_HOTKEY_DEFAULT = "Pause"

# Every persisted config key with its empty/default value. Written into the file
# on startup (and on every save) so reading config.json shows every knob.
_DISCOVERABLE_DEFAULTS: dict[str, object] = {
    "samples_dir": "",
    "images_dir": "",
    "scorer_email": "",
    "browser_command": "",
    CLEAR_ON_SUBMIT_KEY: CLEAR_ON_SUBMIT_DEFAULT,
    "pause_timer_hotkey": PAUSE_TIMER_HOTKEY_DEFAULT,
    "task_selection": {},
}


class UserConfig(TypedDict):
    """All fields optional so partial configs round-trip cleanly."""

    samples_dir: NotRequired[str]
    images_dir: NotRequired[str]
    scorer_email: NotRequired[str]
    # Shell-style command for opening the scoring UI URL. The URL is appended as
    # the final argument, or substituted into a ``{url}`` placeholder.
    browser_command: NotRequired[str]
    clear_on_submit: NotRequired[ClearOnSubmit]
    pause_timer_hotkey: NotRequired[str]
    # Global task selection: task_name -> selected.
    task_selection: NotRequired[dict[str, bool]]
    # Pending change descriptions waiting to be shown in the UI startup banner.
    changed: NotRequired[list[str]]


def config_dir() -> Path:
    """User config dir for this tool (honors ``$XDG_CONFIG_HOME``)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / _APP_DIR_NAME


def config_path() -> Path:
    return config_dir() / _CONFIG_FILENAME


def load_config() -> UserConfig:
    """Return the saved config, or an empty dict if nothing is saved yet."""
    path = config_path()
    if not path.exists():
        return cast(UserConfig, {})
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise TypeError(f"{path} is not a JSON object")
    return cast(UserConfig, raw)


def save_config(config: UserConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged: dict[str, object] = {**_DISCOVERABLE_DEFAULTS, **config}
    with path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, sort_keys=True)


def ensure_defaults_persisted() -> None:
    """Write every discoverable default to the config file if missing."""
    config = load_config()
    raw = cast(dict[str, object], config)
    if all(k in raw for k in _DISCOVERABLE_DEFAULTS):
        return
    save_config(config)


def record_changes(messages: list[str]) -> None:
    """Append change descriptions to the persisted ``changed`` list."""
    if not messages:
        return
    config = load_config()
    existing = list(config.get("changed", []))
    existing.extend(messages)
    config["changed"] = existing
    save_config(config)


def drain_changes() -> list[str]:
    """Return and clear the pending change descriptions."""
    config = load_config()
    changes = list(config.get("changed", []))
    if not changes:
        return []
    raw = cast(dict[str, object], config)
    raw.pop("changed", None)
    save_config(cast(UserConfig, raw))
    return changes


def update_config(**fields: object) -> None:
    """Merge ``fields`` into the saved config and record any actual changes."""
    config = load_config()
    diffs: list[str] = []
    for key, new_value in fields.items():
        old_value = cast(dict[str, object], config).get(key)
        if old_value == new_value:
            continue
        diffs.append(format_scalar_change(key, new_value))
    config.update(cast(UserConfig, fields))
    if diffs:
        existing = list(config.get("changed", []))
        existing.extend(diffs)
        config["changed"] = existing
    save_config(config)


def format_scalar_change(key: str, value: object) -> str:
    """Render a config change for the startup banner."""
    label = {
        "samples_dir": "Samples dir",
        "images_dir": "Images dir",
        "scorer_email": "Scorer",
        "browser_command": "Browser command",
        "clear_on_submit": "Clear-on-submit",
        "pause_timer_hotkey": "Pause-timer hotkey",
    }.get(key, key)
    return f"{label}: {value}"


def format_task_selection_changes(old: dict[str, bool], new: dict[str, bool]) -> list[str]:
    """Summarize task_selection diffs as up to two lines for the banner."""
    selected: list[str] = []
    deselected: list[str] = []
    for name, value in new.items():
        if old.get(name) == value:
            continue
        (selected if value else deselected).append(name)
    selected.sort()
    deselected.sort()
    messages: list[str] = []
    if selected:
        messages.append(f"Selected tasks: {', '.join(selected)}")
    if deselected:
        messages.append(f"Deselected tasks: {', '.join(deselected)}")
    return messages


# ---------------------------------------------------------------------------
# Resolved accessors
# ---------------------------------------------------------------------------


def pause_timer_hotkey() -> str:
    """Resolved KeyboardEvent.key value, or empty string for disabled."""
    raw = cast(dict[str, object], load_config()).get("pause_timer_hotkey")
    if raw is None:
        return PAUSE_TIMER_HOTKEY_DEFAULT
    return str(raw)


def clear_on_submit_mode() -> ClearOnSubmit:
    """Resolved clear-on-submit setting, defaulting to ``"scores"``."""
    raw = cast(dict[str, object], load_config()).get(CLEAR_ON_SUBMIT_KEY)
    return raw if raw in CLEAR_ON_SUBMIT_VALUES else CLEAR_ON_SUBMIT_DEFAULT


def default_samples_dir() -> Path:
    """The samples dir used when nothing else is specified (``data/samples``).

    Paired with the images-dir default of ``<samples-dir>/../images``, this puts
    samples at ``data/samples`` and images at ``data/images``.
    """
    return Path.cwd() / "data" / "samples"


def resolve_samples_dir(cli_value: Path | None) -> Path:
    """Pick the samples dir: explicit CLI > saved config > ./samples (persisted)."""
    if cli_value is not None:
        return cli_value.expanduser().resolve()
    saved = load_config().get("samples_dir")
    if saved:
        return Path(saved)
    chosen = default_samples_dir()
    update_config(samples_dir=str(chosen))
    return chosen


def resolve_images_dir(cli_value: Path | None, samples_dir: Path) -> Path:
    """Pick the images dir: explicit CLI > saved config > ``<samples-dir>/../images``."""
    if cli_value is not None:
        return cli_value.expanduser().resolve()
    saved = load_config().get("images_dir")
    if saved:
        return Path(saved)
    return (samples_dir.parent / "images").resolve()


def _git_user_email() -> str:
    result = subprocess.run(
        ["git", "config", "user.email"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_scorer(cli_value: str) -> str:
    """Resolve the scorer identity: ``--scorer`` > config > git user.email > username.

    The resolved value is persisted so ``config show`` reflects it and later runs
    are stable.
    """
    if cli_value:
        update_config(scorer_email=cli_value)
        return cli_value
    saved = load_config().get("scorer_email")
    if saved:
        return saved
    import getpass

    resolved = _git_user_email() or getpass.getuser() or "unknown"
    update_config(scorer_email=resolved)
    return resolved


def open_url_in_browser(url: str) -> None:
    """Launch ``url`` in the configured browser, or the system default."""
    cmd = (load_config().get("browser_command") or "").strip()
    if not cmd:
        webbrowser.open(url)
        return

    parts = shlex.split(cmd, posix=True)
    if not parts:
        webbrowser.open(url)
        return

    argv = (
        [p.replace("{url}", url) for p in parts]
        if any("{url}" in p for p in parts)
        else [*parts, url]
    )
    try:
        subprocess.Popen(argv)
    except OSError as e:
        raise RuntimeError(f"browser_command failed: {e} (argv: {argv})") from e


# ---------------------------------------------------------------------------
# `config` subcommand
# ---------------------------------------------------------------------------

# Flat scalar keys settable via `config set KEY VALUE`, derived from
# `_DISCOVERABLE_DEFAULTS` (dict insertion order) minus `task_selection`, which is
# UI-managed (not set via the flat form).
_NESTED_CONFIG_KEYS = frozenset({"task_selection"})
CONFIG_KEYS: tuple[str, ...] = tuple(
    key for key in _DISCOVERABLE_DEFAULTS if key not in _NESTED_CONFIG_KEYS
)


def config_main(argv: list[str]) -> None:
    """Entry point for ``local-manual-score config ...`` subcommands."""
    parser = argparse.ArgumentParser(
        prog="local-manual-score config",
        description="Inspect or modify the local-manual-score user config.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="Print the config file path and its contents.")
    sub.add_parser("path", help="Print the config file path (one line).")
    sub.add_parser("edit", help="Open the config file in $EDITOR.")
    set_p = sub.add_parser("set", help="Set a config field.")
    set_p.add_argument(
        "key",
        help=f"One of: {', '.join(CONFIG_KEYS)}",
    )
    set_p.add_argument("value")
    unset_p = sub.add_parser("unset", help="Remove a config field so its default is used again.")
    unset_p.add_argument("key")

    args = parser.parse_args(argv)
    if args.action == "show":
        config_show()
    elif args.action == "path":
        print(config_path())
    elif args.action == "edit":
        config_edit()
    elif args.action == "set":
        config_set(args.key, args.value)
    elif args.action == "unset":
        config_unset(args.key)


def config_show() -> None:
    path = config_path()
    print(f"Config file: {path}")
    if not path.exists():
        print("(no config saved yet)")
        return
    print(path.read_text(encoding="utf-8"))


def config_edit() -> None:
    path = config_path()
    if not path.exists():
        save_config(cast(UserConfig, {}))
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        editor = "notepad" if sys.platform == "win32" else "vi"
    subprocess.run([editor, str(path)], check=False)


def config_set(key: str, value: str) -> None:
    if key not in CONFIG_KEYS:
        allowed = ", ".join(CONFIG_KEYS)
        raise SystemExit(f"Unknown key {key!r}. Settable keys: {allowed}")

    parsed: object = value
    if key in ("samples_dir", "images_dir"):
        parsed = str(Path(value).expanduser().resolve())
    elif key == CLEAR_ON_SUBMIT_KEY and value not in CLEAR_ON_SUBMIT_VALUES:
        allowed = ", ".join(CLEAR_ON_SUBMIT_VALUES)
        raise SystemExit(f"Invalid value {value!r} for {key}; must be one of: {allowed}")
    update_config(**{key: parsed})
    print(f"Set {key} = {parsed}")
    print(f"Saved to {config_path()}")


def config_unset(key: str) -> None:
    raw = cast(dict[str, object], load_config())
    if key not in raw:
        print(f"{key} was not set; nothing to do.")
        return
    del raw[key]
    save_config(cast(UserConfig, raw))
    print(f"Unset {key}")
    print(f"Saved to {config_path()}")
