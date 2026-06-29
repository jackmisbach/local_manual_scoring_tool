"""Flask app for the manual scoring UI."""

# pyright: reportUnusedFunction=false

import math
import os
import platform
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, redirect, render_template, request
from flask.typing import ResponseReturnValue
from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markupsafe import Markup

from scoring.config import (
    clear_on_submit_mode,
    config_path,
    drain_changes,
    general_guidance_url,
    pause_timer_hotkey,
    task_guidance_url,
)
from scoring.infra import (
    INFO_ROW,
    MANUAL_SCORE_SCORER,
    SCORER_NAME_METADATA_KEY,
    SCORING_VERSION,
    container_is_running,
    list_running_scoring_containers,
    open_vscode_in_container,
    setup_display,
    stop_all_scoring_containers,
    stop_container,
)
from scoring.state import (
    AdvanceResult,
    ScoringState,
    local_sample_info,
    next_pending_index,
)
from scoring.store import (
    EXISTING_METADATA_DISPLAY,
    SavedScore,
    ScoreEventScore,
    ScoreFile,
    ScoreFormData,
    TimerState,
    add_invalidation,
    append_history,
    clear_finished,
    clear_stash,
    derive_queue_status,
    entries_equal_ignoring_saved_at,
    has_finish_event,
    has_history_for_scorer,
    history_for_scorer,
    history_path,
    invalidated_local_uuids,
    last_activity_map,
    load_score_file,
    load_scratchpad,
    load_timer_state,
    local_event_uuid_of,
    most_invested_entry_for_scorer,
    new_local_event_uuid,
    now_iso,
    queue_summary_fields,
    save_scratchpad,
    save_stash,
    save_timer_state,
    set_finished,
    set_skipped,
    stash_matches_submission,
)
from scoring.task_selection import load_selection, save_selection


def _validate_bounded_float(
    raw: object, name: str, minimum: float, maximum: float | None
) -> tuple[float | None, tuple[Response, int] | None]:
    """Parse a form value into a finite float within ``[minimum, maximum]``.

    Returns ``(value, None)`` on success, or ``(None, error_response)`` if the
    field is unparseable, non-finite, or outside the range.
    """
    try:
        v = float(str(raw))
    except (ValueError, TypeError):
        return None, (jsonify(success=False, error=f"Invalid {name} value"), 400)
    if not math.isfinite(v):
        return None, (jsonify(success=False, error=f"{name} must be finite"), 400)
    if v < minimum or (maximum is not None and v > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
        return None, (jsonify(success=False, error=f"{name} must be {bound}"), 400)
    return v, None


def _clean_explanation(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw)
    return text if text.strip() else None


def _parse_score_payload(
    data: object,
    require_complete: bool,
) -> tuple[ScoreFormData, tuple[Response, int] | None]:
    """Validate the score-form payload, building a ScoreFormData dict.

    Returns ``(ScoreFormData, None)`` on success or
    ``({}, (error_response, 400))`` on failure. Missing or empty fields are
    simply absent from the result; range violations short-circuit with a 400.
    When ``require_complete`` is True, value/minutes/explanation must all be
    present.
    """
    if not isinstance(data, dict):
        return {}, (jsonify(success=False, error="Invalid request"), 400)

    out: ScoreFormData = {}
    raw_value = data.get("value")
    if raw_value not in (None, ""):
        value, err = _validate_bounded_float(raw_value, "value", 0.0, 1.0)
        if err is not None:
            return {}, err
        assert value is not None
        out["value"] = value

    raw_minutes = data.get("minutes_to_score")
    if raw_minutes not in (None, ""):
        minutes, err = _validate_bounded_float(raw_minutes, "minutes_to_score", 0.0, None)
        if err is not None:
            return {}, err
        assert minutes is not None
        out["minutes_to_score"] = minutes

    explanation = _clean_explanation(data.get("explanation"))
    if explanation is not None:
        out["explanation"] = explanation

    if require_complete:
        if "value" not in out:
            return {}, (jsonify(success=False, error="Missing value"), 400)
        if "minutes_to_score" not in out:
            return {}, (jsonify(success=False, error="Missing minutes_to_score"), 400)
        if "explanation" not in out:
            return {}, (jsonify(success=False, error="Missing explanation"), 400)

    return out, None


def _form_to_score_event(
    form: ScoreFormData,
    *,
    author_email: str,
    scored_at: str | None = None,
    local_event_uuid: str | None = None,
) -> SavedScore:
    """Convert the form payload into a ScoreEvent-shaped SavedScore.

    Maps value/explanation to the ScoreEvent's top-level ``score.value`` /
    ``score.explanation`` and minutes_to_score into
    ``score.metadata.minutes_spent_scoring``. ``author``, ``scoring_version``,
    and ``scored_at`` are always written into metadata so a single ScoreEvent
    is self-describing without consulting any other row. A ``local_event_uuid``
    (passed only on finalized submits) is written into metadata too.

    Missing fields are simply not written into metadata, so the same builder
    serves both the finalized submit and the all-optional stash path.
    """
    metadata: dict[str, Any] = {
        "author": author_email,
        "scoring_version": SCORING_VERSION,
        "scored_at": scored_at or now_iso(),
        SCORER_NAME_METADATA_KEY: INFO_ROW,
    }
    if "minutes_to_score" in form:
        metadata["minutes_spent_scoring"] = form["minutes_to_score"]
    if local_event_uuid is not None:
        metadata["local_event_uuid"] = local_event_uuid

    score: ScoreEventScore = {
        "answer": None,
        "history": [],
        "metadata": metadata,
    }
    if "value" in form:
        score["value"] = form["value"]
    if "explanation" in form:
        score["explanation"] = form["explanation"]

    entry: SavedScore = {
        "scorer": MANUAL_SCORE_SCORER,
        "score": score,
    }
    return entry


def _score_event_to_form(saved: SavedScore | None) -> ScoreFormData | None:
    """Inverse of :func:`_form_to_score_event` for pre-filling the form.

    Used by the index route to feed prior stashes / history into the form
    when the user reopens a sample. Missing fields stay missing (the form
    treats them as empty). Returns ``None`` for a ``None`` input so callers
    can chain without if-elses.
    """
    if saved is None:
        return None
    score = saved.get("score") or {}
    meta = score.get("metadata") or {}
    form: ScoreFormData = {}
    if "value" in score:
        form["value"] = score["value"]
    if "explanation" in score:
        form["explanation"] = score["explanation"]
    if "minutes_spent_scoring" in meta:
        form["minutes_to_score"] = meta["minutes_spent_scoring"]
    return form


def _saved_score_for_template(
    saved: SavedScore | None,
    sample_id: str | None = None,
) -> dict[str, Any] | None:
    """Render a SavedScore for the template/JS by exposing a flat form view.

    The template's pre-fill JS reads the flat value/explanation/minutes
    fields. We attach them alongside the raw ScoreEvent payload so
    client-side dedup + the "prior scores" panel can pull whichever they need.

    When ``sample_id`` is given, also expose the resolved ``local_event_uuid``
    (stored or derived) so the client can invalidate this exact entry.
    """
    if saved is None:
        return None
    form = _score_event_to_form(saved) or {}
    out: dict[str, Any] = {**saved, **form}
    if sample_id is not None:
        out["local_event_uuid"] = local_event_uuid_of(saved, sample_id)
    return out


def _max_prior_minutes(
    score_file: ScoreFile | None,
    scorer_email: str | None,
) -> float | None:
    """Highest ``minutes_spent_scoring`` from any local history entry by ``scorer_email``.

    Routes through :func:`most_invested_entry_for_scorer` so the
    "most-invested" selector and the warn-if-lower-than-prior gate agree
    on which entry counts as the high-water mark. Returns ``None`` when
    no priors exist or none carry a numeric minutes field.
    """
    if score_file is None or scorer_email is None:
        return None
    entry = most_invested_entry_for_scorer(score_file, scorer_email)
    if entry is None:
        return None
    meta = (entry.get("score") or {}).get("metadata") or {}
    raw = meta.get("minutes_spent_scoring")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    return None


# Trailing explicit anchor on a heading line, e.g. ``## Background {#background}``.
# Google Docs' markdown export emits these for table-of-contents links, but
# markdown-it's default mode renders the braces literally and adds no id, so the
# in-page ToC links land nowhere.
_HEADING_ANCHOR_RE = re.compile(r"\s*\{#([^}]+)\}\s*$")


def apply_heading_anchors(state: StateCore) -> None:
    """Promote trailing ``{#anchor}`` markers on headings to real ``id`` attributes.

    Lets the table-of-contents links Google Docs exports actually jump to their
    headings, and strips the literal braces from the rendered heading text.
    """
    tokens = state.tokens
    for i, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        inline = tokens[i + 1]
        match = _HEADING_ANCHOR_RE.search(inline.content)
        if match is None:
            continue
        token.attrSet("id", match.group(1))
        inline.content = _HEADING_ANCHOR_RE.sub("", inline.content)
        for child in reversed(inline.children or []):
            if child.type == "text":
                child.content = _HEADING_ANCHOR_RE.sub("", child.content)
                break


def open_in_os_default(path: str) -> None:
    """Open ``path`` (a file or folder) with the OS default application."""
    system = platform.system()
    if system == "Windows":
        os.startfile(path)  # pyright: ignore[reportAttributeAccessIssue]
    elif system == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def create_app(
    state: ScoringState,
    lock: threading.Lock,
    advance_fn: Callable[[], AdvanceResult],
    rebuild_queue_fn: Callable[[], None],
) -> Flask:
    app = Flask(__name__)
    md = MarkdownIt("default")
    md.core.ruler.push("heading_anchors", apply_heading_anchors)

    def _markdown_filter(text: str | None) -> Markup:
        return Markup(md.render(text or ""))

    app.jinja_env.filters["markdown"] = _markdown_filter

    @app.before_request
    def block_writes_during_exit() -> tuple[Response, int] | None:
        # Once exit_app has fired, the process is about to die. Block
        # mutating requests so a late write (timer-state sendBeacons on
        # pagehide, in-flight scratchpad saves, etc.) can't land
        # half-written while the daemon exit thread tears the process
        # down. Read-only routes still serve so the page can poll until
        # the process dies.
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if not request.path.startswith("/api/"):
            return None
        with lock:
            exiting = state.get("exiting", False)
        if not exiting:
            return None
        return jsonify(success=False, error="Tool is shutting down."), 503

    @app.route("/")
    def index() -> ResponseReturnValue:
        with lock:
            pending_selection = state.get("pending_task_selection", False)
            pending_summary = state.get("pending_summary", False)
            uuid = state["uuid"]
            info = state["info"]
            container_name = state["container_name"]
            queue = list(state["queue"])
            current_idx = state["current_idx"]
            setup_status_now = state["setup_state"].get("status", "idle")
            attach = state["attach"]
            cli_no_container = state["cli_no_container"]
            samples_dir = state["samples_dir"]
            scorer_email = state["scorer_email"]

        if pending_selection:
            # Defer the config-changes drain so the modal save itself
            # (a config mutation) gets included in the banner shown on the
            # subsequent scoring-page render.
            return render_template("task_selection.html", standalone=True)
        # An empty queue means "nothing to score" — for example after the user
        # deselects every task with pending samples, or when the selected tasks
        # contain only samples that aren't set up for manual scoring. /summary
        # renders that state clearly and runs its own drain. pending_summary
        # covers the startup case where the whole eval set was already
        # processed; cleared by /api/navigate once the user picks a row to open.
        if not queue or pending_summary:
            return redirect("/summary")
        # Pre-fill the form from the on-disk score file. Stash takes
        # priority over history so an in-progress draft isn't masked by a
        # previously-submitted entry. Falling back to the current scorer's
        # most-invested submission (max ``minutes_spent_scoring``) means a
        # quick revision after a thorough first pass doesn't eclipse the
        # original work — Reload lands the user back on the entry they spent
        # the most time on.
        score_file = load_score_file(samples_dir, uuid)
        # Two separate pre-fill sources so the UI can offer both Restore
        # (stash → form) and Reload (most-invested history → form)
        # independently. The client picks stash-first for the initial
        # page-load pre-fill; the buttons let the user switch between
        # them later. The full ``history_entries`` list is also passed
        # so the client can dedup the Submit button against every prior
        # entry, not just the most-invested one.
        stash: SavedScore | None = None
        most_invested_history: SavedScore | None = None
        history_entries: list[SavedScore] = []
        has_local_history = False
        has_stash = score_file is not None and score_file.get("stash") is not None
        finish_recorded = has_finish_event(score_file)
        # Per-user filtering matches the full email recorded in
        # ``score.metadata.author``.
        if score_file is not None:
            has_local_history = has_history_for_scorer(score_file, scorer_email)
            stash = score_file.get("stash")
            most_invested_history = most_invested_entry_for_scorer(score_file, scorer_email)
            history_entries = history_for_scorer(score_file, scorer_email)
        prior_max_minutes = _max_prior_minutes(score_file, scorer_email)
        invalidated_uuids = sorted(invalidated_local_uuids(score_file))
        config_changes = drain_changes()
        container_exists = container_is_running(container_name)
        if setup_status_now == "error":
            initial_section = "setup-error"
        elif setup_status_now in ("downloading", "pulling", "starting"):
            initial_section = "setup-progress"
        elif container_exists:
            initial_section = "container-ready"
        else:
            initial_section = "container-idle"
        scorable_total = len(queue)
        queue_done = sum(1 for s in queue if s["status"] == "done")
        queue_skipped = sum(1 for s in queue if s["status"] == "skipped")
        return render_template(
            "scoring.html",
            uuid=uuid,
            info=info,
            container_name=container_name,
            queue=queue,
            queue_done=queue_done,
            queue_skipped=queue_skipped,
            queue_total=scorable_total,
            show_queue_progress=len(queue) > 1,
            current_idx=current_idx,
            initial_section=initial_section,
            general_guidance_url=general_guidance_url(),
            task_guidance_url=task_guidance_url(info["task_name"]),
            task_guidance_label=(info["task_name"].split("/")[-1] or "Task") + " guidance",
            stash=_saved_score_for_template(stash, sample_id=uuid),
            most_invested_history=_saved_score_for_template(most_invested_history, sample_id=uuid),
            history_entries=[_saved_score_for_template(e, sample_id=uuid) for e in history_entries],
            has_local_history=has_local_history,
            has_stash=has_stash,
            finish_recorded=finish_recorded,
            prior_max_minutes=prior_max_minutes,
            invalidated_uuids=invalidated_uuids,
            clear_on_submit=clear_on_submit_mode(),
            pause_timer_hotkey=pause_timer_hotkey(),
            scratchpad_content=load_scratchpad(samples_dir, uuid),
            timer_state=load_timer_state(samples_dir, uuid) or {},
            scorer_email=scorer_email,
            existing_metadata_display=list(EXISTING_METADATA_DISPLAY),
            attach=attach,
            cli_no_container=cli_no_container,
            config_changes=config_changes,
        )

    @app.route("/tasks")
    def task_selection_page() -> ResponseReturnValue:
        """Standalone reopenable task-selection page (footer "Tasks…" button).

        Distinct from the auto-shown modal at startup: ``standalone=False``
        lets the template show a Cancel button so the user can back out
        without changing their selection. Renders even when this session
        has no tasks to choose from — the template shows a "No tasks
        found" message in that case, which is more helpful than a 400.
        """
        return render_template("task_selection.html", standalone=False)

    @app.route("/api/tasks", methods=["GET"])
    def get_tasks() -> ResponseReturnValue:
        """Global task selection state for the modal.

        Returns each task in the current session (union across every
        eval-set we opened), with the per-task sample count and the
        user's stored decision. Tasks the user has never decided on
        default to selected, matching the modal's first-appearance UX.
        """
        with lock:
            all_tasks = list(state["all_tasks"])
            task_counts = dict(state["task_counts"])

        selection = load_selection()
        unseen: list[str] = []
        tasks_payload: list[dict[str, Any]] = []
        for name in all_tasks:
            is_unseen = name not in selection
            if is_unseen:
                unseen.append(name)
            tasks_payload.append(
                {
                    "name": name,
                    "sample_count": task_counts.get(name, 0),
                    # First-appearance default per the spec: all-selected so
                    # the existing flow stays unchanged for users who don't
                    # opt in.
                    "selected": selection.get(name, True),
                    "unseen": is_unseen,
                }
            )

        return jsonify(tasks=tasks_payload, unseen=unseen)

    @app.route("/api/tasks", methods=["POST"])
    def post_tasks() -> ResponseReturnValue:
        """Persist the global task selection and rebuild the queue.

        Body: ``{"tasks": {"<task_name>": true|false, ...}}``. Saves to the
        per-user config so the same decision carries to every later
        eval-set the user opens; ``rebuild_queue_fn`` re-runs the score
        check against the updated selection.
        """
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(success=False, error="Invalid request"), 400
        tasks = data.get("tasks")
        if not isinstance(tasks, dict):
            return jsonify(success=False, error="Missing tasks map"), 400

        normalized: dict[str, bool] = {str(k): bool(v) for k, v in tasks.items()}
        save_selection(normalized)

        rebuild_queue_fn()

        # The selection step records the user's intent; whether the resulting
        # queue is empty (e.g. all selected tasks are already scored, or
        # their samples aren't set up for manual scoring) is the index
        # route's concern — it redirects to /summary in that case.
        return jsonify(success=True)

    @app.route("/api/setup-status")
    def setup_status() -> ResponseReturnValue:
        with lock:
            result = dict(state["setup_state"])
            container_name = state["container_name"]
            queue = list(state["queue"])
            result["queue"] = {
                "current": state["current_idx"] + 1,
                "total": len(queue),
                "done": sum(1 for s in queue if s["status"] in ("done", "skipped")),
            }
        running = list_running_scoring_containers()
        result["container_exists"] = container_name in running
        prefix = "manual-score-"
        others: list[dict[str, Any]] = []
        for name in running:
            if name == container_name:
                continue
            uuid_prefix = name[len(prefix) :] if name.startswith(prefix) else ""
            match_idx = next(
                (
                    i
                    for i, e in enumerate(queue)
                    if uuid_prefix and e["uuid"].startswith(uuid_prefix)
                ),
                None,
            )
            if match_idx is not None:
                entry = queue[match_idx]
                others.append(
                    {
                        "container_name": name,
                        "queue_index": match_idx,
                        "uuid": entry["uuid"],
                        "task_name": entry["task_name"],
                    }
                )
            else:
                others.append(
                    {
                        "container_name": name,
                        "queue_index": None,
                        "uuid": "",
                        "task_name": "",
                    }
                )
        result["other_containers"] = others
        return jsonify(result)

    def sync_sample_state(uuid: str) -> None:
        """Re-derive the in-memory queue entry for ``uuid`` from disk.

        Called by every route that changes a sample's saved content (submit,
        stash, drop-stash, finish, reopen). Reads the sample's score file once
        and updates the matching queue entry's status and summary columns
        (score / explanation / minutes / ``has_stash``).

        Status comes from :func:`derive_queue_status` over the score file and
        the sample's skip flag, so a real score (history/finish) takes
        precedence and skip / submit / stash never need to touch each other.
        Caller must hold ``lock``.
        """
        samples_dir = state["samples_dir"]
        score_file = load_score_file(samples_dir, uuid)
        status = derive_queue_status(
            score_file,
            skipped=uuid in state["skipped"],
            include_skipped=state["include_skipped"],
            include_done=state["include_done"],
        )
        summary = queue_summary_fields(score_file)
        for entry in state["queue"]:
            if entry["uuid"] != uuid:
                continue
            entry["status"] = status
            for key in ("score", "explanation", "minutes_to_score", "has_stash"):
                if key in summary:
                    entry[key] = summary[key]  # type: ignore[literal-required]
                else:
                    entry.pop(key, None)  # type: ignore[misc]
            break

    @app.route("/api/submit-score", methods=["POST"])
    def submit_score() -> ResponseReturnValue:
        form, err = _parse_score_payload(
            request.get_json(),
            require_complete=True,
        )
        if err is not None:
            return err
        # require_complete=True guarantees value and explanation are present,
        # so the ScoreEvent payload built below has both.
        assert (
            "value" in form
        ), "_parse_score_payload(require_complete=True) did not produce a complete form"
        assert (
            "explanation" in form
        ), "_parse_score_payload(require_complete=True) did not produce a complete form"

        with lock:
            uuid = state["uuid"]
            samples_dir = state["samples_dir"]
            scorer_email = state["scorer_email"]

        # Tag every finalized score with a fresh local event uuid so it can be
        # invalidated and joined on import. (Drafts/stashes get none.)
        entry = _form_to_score_event(
            form,
            author_email=scorer_email,
            local_event_uuid=new_local_event_uuid(),
        )

        # Dedupe: a no-op resubmit (every score field identical to a prior
        # history entry, ignoring volatile metadata like ``scored_at``)
        # shouldn't grow the file.
        existing = load_score_file(samples_dir, uuid)
        if existing is not None and any(
            entries_equal_ignoring_saved_at(prior, entry) for prior in existing["history"]
        ):
            return jsonify(
                success=False,
                error="This score matches a previous submission — edit a field to submit a new entry.",
            ), 400

        # Only drop the in-progress draft when the user submitted exactly what
        # they had stashed. A submission that differs is a separate event —
        # keep the draft so iteration isn't lost.
        stash_dropped = stash_matches_submission((existing or {}).get("stash"), entry)
        append_history(samples_dir, uuid, entry, drop_stash=stash_dropped)
        # No export write here: the per-sample score logs are a generated export,
        # rebuilt on demand from this history by ``local-manual-score export``
        # (see scoring.export).

        # Recompute prior_max_minutes including the entry we just appended so
        # the client can warn on the next submission if its minutes drop below.
        prior_max_minutes = _max_prior_minutes(load_score_file(samples_dir, uuid), scorer_email)

        has_next = False
        with lock:
            sync_sample_state(uuid)
            has_next = next_pending_index(state["queue"], state["current_idx"]) is not None
            queue_done = sum(1 for s in state["queue"] if s["status"] == "done")
            queue_skipped = sum(1 for s in state["queue"] if s["status"] == "skipped")
            scorable_total = len(state["queue"])
        return jsonify(
            success=True,
            has_next=has_next,
            saved_entry=_saved_score_for_template(entry, sample_id=uuid),
            score_path=str(history_path(samples_dir)),
            queue_done=queue_done,
            queue_skipped=queue_skipped,
            queue_total=scorable_total,
            prior_max_minutes=prior_max_minutes,
            stash_dropped=stash_dropped,
        )

    @app.route("/api/finish", methods=["POST"])
    def finish_sample() -> ResponseReturnValue:
        """Record a "Done Scoring This Sample" event for the current sample.

        Saved locally in the sample's record; this marks the sample ``done`` so
        navigation stops landing on it (re-surface with ``--include-done``).
        """
        with lock:
            uuid = state["uuid"]
            samples_dir = state["samples_dir"]
        set_finished(samples_dir, uuid)
        with lock:
            sync_sample_state(uuid)
        return jsonify(success=True)

    @app.route("/api/reopen", methods=["POST"])
    def reopen_sample() -> ResponseReturnValue:
        """Clear the finished mark so the sample can be re-scored."""
        with lock:
            uuid = state["uuid"]
            samples_dir = state["samples_dir"]
        clear_finished(samples_dir, uuid)
        with lock:
            sync_sample_state(uuid)
        return jsonify(success=True)

    @app.route("/api/scores", methods=["GET"])
    def get_my_scores() -> ResponseReturnValue:
        """Return the current user's local history for one sample as JSON.

        Defaults to the sample currently being scored; pass ``?uuid=`` to
        target any other sample under the same artifacts dir. Filtering
        matches ``score.metadata.author`` against the user's full email.

        Returns the same combined view the page-load template uses
        (ScoreEvent shape + spread flat form fields), so a client can drop
        the payload straight into the prior-scores panel without
        reshaping.
        """
        with lock:
            uuid = request.args.get("uuid") or state["uuid"]
            samples_dir = state["samples_dir"]
            scorer_email = state["scorer_email"]
        score_file = load_score_file(samples_dir, uuid)
        entries = history_for_scorer(score_file, scorer_email)
        return jsonify(
            success=True,
            uuid=uuid,
            scorer_email=scorer_email,
            entries=[_saved_score_for_template(e, sample_id=uuid) for e in entries],
            invalidated=sorted(invalidated_local_uuids(score_file)),
            finished_at=(score_file or {}).get("finished_at"),
        )

    @app.route("/api/invalidate", methods=["POST"])
    def invalidate_score() -> ResponseReturnValue:
        """Invalidate a finalized score on the current sample by its event uuid.

        Body ``{"local_event_uuid": str, "reason"?: str}``. Persists an
        invalidation record (idempotent per target uuid) so the UI can grey out
        the score and the export emits an ``event_invalidation`` info line. Under
        ``/api/``, so the exit-guard already blocks it while shutting down.
        """
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(success=False, error="Invalid request"), 400
        local_event_uuid = data.get("local_event_uuid")
        if not isinstance(local_event_uuid, str) or not local_event_uuid:
            return jsonify(success=False, error="Missing local_event_uuid"), 400
        reason = str(data.get("reason") or "")
        with lock:
            uuid = state["uuid"]
            samples_dir = state["samples_dir"]
            scorer_email = state["scorer_email"]
        add_invalidation(samples_dir, uuid, local_event_uuid, scorer_email, reason)
        return jsonify(success=True)

    def advance_response() -> ResponseReturnValue:
        """Run advance_fn and translate its result into a JSON response."""
        result = advance_fn()
        if result == "exhausted":
            return jsonify(success=False, done=True)
        return jsonify(success=True)

    @app.route("/api/next", methods=["POST"])
    def next_sample() -> ResponseReturnValue:
        return advance_response()

    @app.route("/api/skip", methods=["POST"])
    def skip_sample() -> ResponseReturnValue:
        with lock:
            uuid = state["queue"][state["current_idx"]]["uuid"]
            samples_dir = state["samples_dir"]
            # Persist the skip flag and mirror it in memory; the derived status
            # then reads as ``skipped`` (it has no history/finish of its own).
            state["skipped"].add(uuid)
            set_skipped(samples_dir, uuid)
            sync_sample_state(uuid)
        return advance_response()

    @app.route("/api/stash", methods=["POST"])
    def stash_score() -> ResponseReturnValue:
        # Stashes go through the same validation as submit-score, just with
        # every field optional. The form pre-fills from this file the next
        # time the user opens this sample (see the index route).
        form, err = _parse_score_payload(
            request.get_json(),
            require_complete=False,
        )
        if err is not None:
            return err

        with lock:
            uuid = state["uuid"]
            samples_dir = state["samples_dir"]
            scorer_email = state["scorer_email"]
        draft = _form_to_score_event(
            form,
            author_email=scorer_email,
        )
        with lock:
            save_stash(samples_dir, uuid, draft)
            sync_sample_state(uuid)

        return jsonify(success=True, score_path=str(history_path(samples_dir)))

    @app.route("/api/scratchpad", methods=["POST"])
    def save_scratchpad_route() -> ResponseReturnValue:
        """Persist the per-sample scratch pad in the sample's store record.

        UI-only working state — it never lands in the score-log export (that's
        built from the score form fields only). Empty content drops the
        ``scratchpad`` key so a sample with no notes leaves no entry.
        """
        data = request.get_json()
        if not isinstance(data, dict) or "content" not in data:
            return jsonify(success=False, error="Missing content"), 400
        content = str(data["content"])
        with lock:
            uuid = state["uuid"]
            samples_dir = state["samples_dir"]
        save_scratchpad(samples_dir, uuid, content)
        return jsonify(success=True)

    @app.route("/api/timer-state", methods=["POST"])
    def save_timer_state_route() -> ResponseReturnValue:
        """Persist the per-sample stopwatch state inside the sample's record.

        Body shape mirrors :class:`TimerState`: the ``normal`` timer carries
        only ``elapsed_seconds`` (accumulated scoring time). UI-only state.
        Called on load, every pause / resume / reset event, periodically while
        running, and on ``pagehide`` / exit, so a sample switch or restart
        restores the accumulated time. Pass an empty body to clear the saved
        state.
        """
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify(success=False, error="body must be an object"), 400
        new_state: TimerState = {}
        snap = data.get("normal")
        if isinstance(snap, dict):
            elapsed = snap.get("elapsed_seconds")
            if not isinstance(elapsed, int | float) or elapsed < 0:
                return jsonify(success=False, error="normal.elapsed_seconds must be >= 0"), 400
            new_state["normal"] = {"elapsed_seconds": float(elapsed)}
        with lock:
            uuid = state["uuid"]
            samples_dir = state["samples_dir"]
        save_timer_state(samples_dir, uuid, new_state or None)
        return jsonify(success=True)

    @app.route("/api/stash", methods=["DELETE"])
    def drop_stash() -> ResponseReturnValue:
        # Drop the in-progress draft without touching history. The form values
        # stay in the browser so the user can keep editing or just walk away;
        # the queue entry's status falls back to whatever the remaining history
        # (or skip flag) implies.
        with lock:
            uuid = state["uuid"]
            samples_dir = state["samples_dir"]
            clear_stash(samples_dir, uuid)
            sync_sample_state(uuid)
        return jsonify(success=True)

    @app.route("/summary")
    def summary() -> ResponseReturnValue:
        with lock:
            queue = list(state["queue"])
            current_idx = state["current_idx"]
            samples_dir = state["samples_dir"]
            pending_summary = state.get("pending_summary", False)
        updated_at = last_activity_map(samples_dir)
        return render_template(
            "summary.html",
            queue=queue,
            current_idx=current_idx,
            can_navigate=True,
            updated_at=updated_at,
            config_changes=drain_changes(),
            pending_summary=pending_summary,
        )

    @app.route("/api/navigate", methods=["POST"])
    def navigate() -> ResponseReturnValue:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(success=False, error="Invalid request"), 400

        with lock:
            queue = state["queue"]
            idx = state["current_idx"]
            total = len(queue)

        if "index" in data:
            try:
                target = int(data["index"])
            except (ValueError, TypeError):
                return jsonify(success=False, error="Invalid index"), 400
            if not 0 <= target < total:
                return jsonify(success=False, error="Index out of range"), 400
        elif "direction" in data:
            target = (idx - 1) % total if data["direction"] == "prev" else (idx + 1) % total
        else:
            return jsonify(success=False, error="Missing direction or index"), 400

        if target == idx:
            return jsonify(success=True)

        target_uuid = queue[target]["uuid"]

        with lock:
            samples_dir = state["samples_dir"]
        info = local_sample_info(target_uuid, samples_dir)

        with lock:
            state["current_idx"] = target
            state["uuid"] = target_uuid
            state["info"] = info
            # Navigating from the summary doesn't auto-start a container; the
            # page shows a "start container" button.
            state["no_container"] = True
            state["setup_state"] = {"status": "idle"}
            state["container_name"] = f"manual-score-{target_uuid[:12]}"
            state.pop("pending_summary", None)

        return jsonify(success=True)

    @app.route("/api/start-container", methods=["POST"])
    def start_container_route() -> ResponseReturnValue:
        with lock:
            state["no_container"] = False
            state["setup_state"] = {"status": "starting"}
            cn = state["container_name"]
        stop_all_scoring_containers()
        threading.Thread(
            target=state["setup_fn"],
            daemon=True,
        ).start()
        return jsonify(success=True, container_name=cn)

    @app.route("/api/retry-setup", methods=["POST"])
    def retry_setup() -> ResponseReturnValue:
        with lock:
            cn = state["container_name"]
            state["setup_state"] = {"status": "starting"}
        stop_container(cn)
        threading.Thread(
            target=state["setup_fn"],
            daemon=True,
        ).start()
        return jsonify(success=True)

    @app.route("/api/setup-display", methods=["POST"])
    def api_setup_display() -> ResponseReturnValue:
        with lock:
            cn = state["container_name"]
        ok = setup_display(cn)
        if ok:
            return jsonify(success=True, url="http://localhost:6080/vnc.html")
        return jsonify(success=False, error="noVNC setup failed")

    @app.route("/api/open", methods=["POST"])
    def open_target() -> ResponseReturnValue:
        """Open something with the OS default app, chosen by ``target``.

        Body ``{"target": "path"|"artifacts"|"config", "path": <str>}``:

        - ``path`` — an arbitrary path, restricted to under ``samples_dir`` so a
          stray POST can't launch arbitrary files on disk. Powers the clickable
          file-path links the UI shows in status messages. ``path`` is required.
        - ``artifacts`` — the current sample's dir (``samples_dir / uuid``).
        - ``config`` — the per-user config file, via the OS default editor.

        Note ``open-vscode`` stays its own route: it's a container attach via the
        ``code`` CLI, a different mechanism entirely.
        """
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(success=False, error="Invalid request"), 400
        target = data.get("target")

        if target == "path":
            if "path" not in data:
                return jsonify(success=False, error="Missing path"), 400
            with lock:
                base = state["samples_dir"].resolve()
            try:
                path = Path(str(data["path"])).resolve()
            except (OSError, ValueError) as e:
                return jsonify(success=False, error=f"Bad path: {e}"), 400
            try:
                path.relative_to(base)
            except ValueError:
                return jsonify(success=False, error="Path is outside artifacts dir"), 400
            if not path.exists():
                return jsonify(success=False, error="Path does not exist"), 404
            open_in_os_default(str(path))
            return jsonify(success=True)

        if target == "artifacts":
            with lock:
                path = state["samples_dir"] / state["uuid"]
            if not path.is_dir():
                return jsonify(success=False, error="Artifacts not downloaded yet")
            open_in_os_default(str(path.resolve()))
            return jsonify(success=True)

        if target == "config":
            # OS default editor — $EDITOR/$VISUAL would block on a terminal the
            # Flask process doesn't own; open_in_os_default detaches cleanly.
            open_in_os_default(str(config_path().resolve()))
            return jsonify(success=True)

        return jsonify(success=False, error="Unknown target"), 400

    @app.route("/api/stop-other-container", methods=["POST"])
    def stop_other_container() -> ResponseReturnValue:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(success=False, error="Invalid request"), 400
        name = str(data.get("container_name", ""))
        if not name.startswith("manual-score-"):
            return jsonify(success=False, error="Refusing to stop non-scoring container"), 400
        # Wait for docker so the client's next /api/setup-status poll
        # reflects reality.
        stop_container(name)
        return jsonify(success=True)

    @app.route("/api/cleanup", methods=["POST"])
    def cleanup() -> ResponseReturnValue:
        try:
            with lock:
                cn = state["container_name"]
                state["no_container"] = True
                state["setup_state"] = {"status": "idle"}
            stop_container(cn)
            return jsonify(success=True)
        except Exception as e:
            return jsonify(success=False, error=str(e))

    @app.route("/api/open-vscode", methods=["POST"])
    def open_vscode() -> ResponseReturnValue:
        with lock:
            cn = state["container_name"]
            project_dir = state["info"]["project_dir"]
        open_vscode_in_container(cn, project_dir)
        return jsonify(success=True)

    @app.route("/api/toggle", methods=["POST"])
    def toggle_setting() -> ResponseReturnValue:
        """Flip a boolean session setting, chosen by ``setting``.

        Body ``{"setting": "autostart"|"attach"}``:

        - ``autostart`` flips ``cli_no_container`` (whether the next sample
          auto-starts a container).
        - ``attach`` flips ``attach`` (whether opening VS Code attaches).

        Returns ``{success, setting, value}`` with the new boolean.
        """
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(success=False, error="Invalid request"), 400
        setting = data.get("setting")
        field = {"autostart": "cli_no_container", "attach": "attach"}.get(str(setting))
        if field is None:
            return jsonify(success=False, error="Unknown setting"), 400
        with lock:
            value = not state[field]  # type: ignore[literal-required]
            state[field] = value  # type: ignore[literal-required]
        return jsonify(success=True, setting=setting, value=value)

    @app.route("/api/exit", methods=["POST"])
    def exit_app() -> ResponseReturnValue:
        stop_all_scoring_containers()
        with lock:
            # Block further disk-mutating routes (timer-state, scratchpad,
            # score submit) so a late write can't race the exit. In-memory only.
            state["exiting"] = True

        # Exit in a daemon thread after a short delay so this response flushes
        # to the browser (which shows "Exiting...") before the process dies.
        def _exit() -> None:
            time.sleep(0.3)
            os._exit(0)

        threading.Thread(target=_exit, daemon=True).start()
        return jsonify(success=True)

    return app
