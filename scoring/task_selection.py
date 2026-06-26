"""Global task selection: which task names should appear in the queue.

Persists in the per-user :mod:`scoring.config` file (alongside ``scorer_email``
and ``artifacts_dir``) so the same decision carries across every eval-set the
user opens. A task selected once stays selected the next time that task
appears in a different eval-set; a deselected task stays deselected.

The on-disk shape inside ``config.json`` is::

    "task_selection": {"<task_name>": true|false, ...}

A missing entry means "unseen": the UI surfaces the Task Selection modal
whenever the active eval-set contains a task the user hasn't decided on yet.
"""

import threading

from scoring.config import format_task_selection_changes, load_config, save_config

# Serializes the read-modify-write on config.json so concurrent /api/tasks
# saves can't lose an entry the other just wrote.
_selection_lock = threading.Lock()


def load_selection() -> dict[str, bool]:
    """Return ``{task_name: selected}`` from the user config (``{}`` if absent)."""
    return dict(load_config().get("task_selection", {}))


def save_selection(selection: dict[str, bool]) -> None:
    """Persist ``selection`` into ``task_selection`` in the user config.

    Merges with any existing entries instead of replacing so a partial save
    (e.g. for one eval-set's tasks) doesn't drop decisions about tasks from
    other eval-sets the user has touched before.
    """
    with _selection_lock:
        config = load_config()
        current = dict(config.get("task_selection", {}))
        merged = dict(current)
        merged.update(selection)
        diffs = format_task_selection_changes(current, merged)
        config["task_selection"] = merged
        if diffs:
            existing = list(config.get("changed", []))
            existing.extend(diffs)
            config["changed"] = existing
        save_config(config)


def unseen_tasks(selection: dict[str, bool], all_tasks: list[str]) -> list[str]:
    """Tasks present in the eval set but not yet recorded in the user config."""
    return [t for t in all_tasks if t not in selection]


def is_task_selected(selection: dict[str, bool], task_name: str) -> bool:
    """Default to ``False`` for tasks the user hasn't decided on yet.

    Callers should gate the queue build on :func:`unseen_tasks` so the user is
    prompted before any unseen task gets silently filtered out.
    """
    return selection.get(task_name, False)
