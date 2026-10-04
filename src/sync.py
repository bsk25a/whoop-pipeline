"""
Entry point. Pull WHOOP, flatten, write to Drive.

Run modes:
    python -m src.sync                 incremental (default lookback 10 days)
    python -m src.sync --days 180      explicit window
    python -m src.sync --backfill      from BACKFILL_MONTHS ago to now

Why the default window is 10 days and not 1:

WHOOP scores a recovery only once the sleep that produced it closes, and
revises records after the fact. A job that only ever fetched yesterday would
permanently store the unscored version of any record that was late. Re-pulling
a rolling window and upserting costs a few extra requests and makes the store
self-healing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .store import DriveStore, folder_id_from_url
from .transform import (
    DAILY_COLUMNS,
    WORKOUT_COLUMNS,
    build_daily_rows,
    build_workout_rows,
    merge_rows,
)
from .whoop import Token, WhoopAuthError, WhoopClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sync")

DEFAULT_LOOKBACK_DAYS = 10


def _env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync WHOOP data into Google Drive")
    parser.add_argument("--days", type=int, help="Days of history to pull")
    parser.add_argument(
        "--backfill", action="store_true", help="Pull BACKFILL_MONTHS of history"
    )
    args = parser.parse_args()

    whoop_id = _env("WHOOP_CLIENT_ID")
    whoop_secret = _env("WHOOP_CLIENT_SECRET")
    google_id = _env("GOOGLE_CLIENT_ID")
    google_secret = _env("GOOGLE_CLIENT_SECRET")
    google_refresh = _env("GOOGLE_REFRESH_TOKEN")
    folder = folder_id_from_url(_env("DRIVE_FOLDER_ID"))

    log.info("Connecting to Drive folder %s", folder)
    store = DriveStore(google_id, google_secret, google_refresh, folder)
    state = store.read_state()

    # --- resolve the WHOOP token -------------------------------------------
    # First run has nothing in Drive, so bootstrap from the secret. Every run
    # after that uses the rotated token in _state.json, because the secret is
    # already spent the moment the first refresh happens.
    token_data = state.get("whoop_token")
    if token_data:
        token = Token.from_dict(token_data)
        log.info("Loaded rotated WHOOP token from _state.json")
    else:
        seed = _env("WHOOP_REFRESH_TOKEN")
        log.info("No stored token; bootstrapping from WHOOP_REFRESH_TOKEN secret")
        token = Token(access_token="", refresh_token=seed, expires_at=0)

    def persist_token(new_token: Token) -> None:
        state["whoop_token"] = new_token.to_dict()
        store.write_state(state)
        log.info("Persisted rotated WHOOP refresh token")

    client = WhoopClient(whoop_id, whoop_secret, token, persist_token)

    # --- window -------------------------------------------------------------
    end = datetime.now(timezone.utc) + timedelta(days=1)
    if args.backfill:
        months = int(os.environ.get("BACKFILL_MONTHS", "6"))
        start = end - timedelta(days=31 * months)
    elif args.days:
        start = end - timedelta(days=args.days)
    else:
        start = end - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    log.info("Window %s -> %s", start.date(), end.date())

    # --- pull ---------------------------------------------------------------
    try:
        cycles = client.cycles(start, end)
        recoveries = client.recoveries(start, end)
        sleeps = client.sleeps(start, end)
        workouts = client.workouts(start, end)
    except WhoopAuthError as exc:
        log.error("%s", exc)
        return 2

    log.info(
        "Fetched %d cycles, %d recoveries, %d sleeps, %d workouts",
        len(cycles), len(recoveries), len(sleeps), len(workouts),
    )

    if not any([cycles, recoveries, sleeps, workouts]):
        log.warning("No records returned. Nothing written.")
        return 0

    # --- raw archive, grouped by month --------------------------------------
    by_month: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {"cycles": [], "recoveries": [], "sleeps": [], "workouts": []}
    )
    # A recovery has no `start` of its own, and its `created_at` can fall in the
    # following month (it is scored after the sleep closes). File it with the
    # cycle it scores so a month's archive is internally consistent.
    cycle_month = {
        c["id"]: (c.get("start") or "")[:7]
        for c in cycles
        if c.get("id") is not None
    }
    for name, records in (
        ("cycles", cycles), ("recoveries", recoveries),
        ("sleeps", sleeps), ("workouts", workouts),
    ):
        for r in records:
            month = (
                cycle_month.get(r.get("cycle_id"), "")
                or r.get("start")
                or r.get("created_at")
                or ""
            )[:7]
            if len(month) == 7:
                by_month[month][name].append(r)

    for month, payload in sorted(by_month.items()):
        store.merge_raw(month, payload)
        log.info("Wrote raw/%s.json", month)

    # --- flattened tables ---------------------------------------------------
    daily_new = build_daily_rows(cycles, recoveries, sleeps, workouts)
    workouts_new = build_workout_rows(workouts)

    daily = merge_rows(
        store.read_csv("daily.csv"), daily_new, key="date", identity="cycle_id"
    )
    store.write_csv("daily.csv", daily, DAILY_COLUMNS)
    log.info("daily.csv: %d rows (%d updated this run)", len(daily), len(daily_new))

    all_workouts = merge_rows(
        store.read_csv("workouts.csv"), workouts_new, key="workout_id"
    )
    store.write_csv("workouts.csv", all_workouts, WORKOUT_COLUMNS)
    log.info("workouts.csv: %d rows", len(all_workouts))

    # --- profile, occasionally ---------------------------------------------
    try:
        state["profile"] = client.profile()
        state["body_measurement"] = client.body_measurement()
    except Exception as exc:  # non-fatal; these are nice-to-have
        log.warning("Could not refresh profile/body measurement: %s", exc)

    state["last_sync"] = datetime.now(timezone.utc).isoformat()
    state["last_window"] = {"start": start.isoformat(), "end": end.isoformat()}
    state["row_counts"] = {"daily": len(daily), "workouts": len(all_workouts)}
    store.write_state(state)

    log.info("Done. %s", json.dumps(state.get("row_counts")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
