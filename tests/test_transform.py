"""
Tests for the flattening layer.

These run without credentials. They cover the cases that would silently corrupt
a trend rather than throw: unscored records, missing sleep, naps, timezone
rollover, and re-syncing a day that already exists.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.transform import (  # noqa: E402
    build_daily_rows,
    build_workout_rows,
    cycle_date,
    flatten_workout,
    local_date,
    merge_rows,
)

# --------------------------------------------------------------- fixtures

CYCLE = {
    "id": 93845,
    "user_id": 10129,
    "start": "2026-09-23T13:16:00.000Z",
    "end": "2026-09-24T12:04:00.000Z",
    "timezone_offset": "-04:00",
    "score_state": "SCORED",
    "score": {
        "strain": 14.87,
        "kilojoule": 8288.3,
        "average_heart_rate": 74,
        "max_heart_rate": 181,
    },
}

RECOVERY = {
    "cycle_id": 93845,
    "sleep_id": "ec5b1a1f-0000-4000-8000-000000000001",
    "user_id": 10129,
    "score_state": "SCORED",
    "score": {
        "user_calibrating": False,
        "recovery_score": 44.0,
        "resting_heart_rate": 52.0,
        "hrv_rmssd_milli": 41.2,
        "spo2_percentage": 96.5,
        "skin_temp_celsius": 33.8,
    },
}

SLEEP = {
    "id": "ec5b1a1f-0000-4000-8000-000000000001",
    "cycle_id": 93845,
    "user_id": 10129,
    "start": "2026-09-23T04:10:00.000Z",
    "end": "2026-09-23T11:55:00.000Z",
    "timezone_offset": "-04:00",
    "nap": False,
    "score_state": "SCORED",
    "score": {
        "stage_summary": {
            "total_in_bed_time_milli": 27_900_000,      # 465 min
            "total_awake_time_milli": 3_600_000,        # 60 min
            "total_no_data_time_milli": 0,
            "total_light_sleep_time_milli": 12_600_000,  # 210 min
            "total_slow_wave_sleep_time_milli": 5_400_000,  # 90 min
            "total_rem_sleep_time_milli": 6_300_000,    # 105 min
            "sleep_cycle_count": 5,
            "disturbance_count": 9,
        },
        "sleep_needed": {
            "baseline_milli": 27_000_000,               # 450 min
            "need_from_sleep_debt_milli": 2_400_000,    # 40 min
            "need_from_recent_strain_milli": 1_200_000,  # 20 min
            "need_from_recent_nap_milli": 0,
        },
        "respiratory_rate": 15.4,
        "sleep_performance_percentage": 82.0,
        "sleep_consistency_percentage": 61.0,
        "sleep_efficiency_percentage": 87.1,
    },
}

WORKOUT = {
    "id": "aa11bb22-0000-4000-8000-000000000002",
    "user_id": 10129,
    "sport_name": "Running",
    "start": "2026-09-23T13:16:45.000Z",
    "end": "2026-09-23T13:46:49.000Z",
    "timezone_offset": "-04:00",
    "score_state": "SCORED",
    "score": {
        "strain": 10.1,
        "average_heart_rate": 158,
        "max_heart_rate": 182,
        "kilojoule": 1795.0,
        "percent_recorded": 100.0,
        "distance_meter": 5378.1,
        "altitude_gain_meter": 23.0,
        "zone_durations": {
            "zone_zero_milli": 0,
            "zone_one_milli": 120_000,
            "zone_two_milli": 300_000,
            "zone_three_milli": 600_000,
            "zone_four_milli": 720_000,
            "zone_five_milli": 60_000,
        },
    },
}

PENDING_CYCLE = {
    "id": 93846,
    "start": "2026-09-24T12:04:00.000Z",
    "end": None,
    "timezone_offset": "-04:00",
    "score_state": "PENDING_SCORE",
    "score": None,
}


def _only(rows, date):
    return next(r for r in rows if r["date"] == date)


# ------------------------------------------------------------------ tests

def test_local_date_shifts_across_utc_midnight():
    # 01:30 UTC on the 24th is 21:30 on the 23rd in New York.
    assert local_date("2026-09-24T01:30:00.000Z", "-04:00") == "2026-09-23"
    assert local_date("2026-09-24T01:30:00.000Z", None) == "2026-09-24"
    assert local_date(None, "-04:00") is None


def test_daily_row_joins_all_four_collections():
    rows = build_daily_rows([CYCLE], [RECOVERY], [SLEEP], [WORKOUT])
    assert len(rows) == 1
    row = _only(rows, "2026-09-23")

    assert row["day_strain"] == 14.87
    assert row["recovery_score"] == 44.0
    assert row["hrv_rmssd_milli"] == 41.2
    assert row["sleep_id"] == SLEEP["id"]
    assert row["workout_count"] == 1
    assert row["workout_sports"] == "Running"


def test_sleep_stage_conversion_and_derived_ratios():
    row = _only(build_daily_rows([CYCLE], [RECOVERY], [SLEEP], []), "2026-09-23")

    assert row["in_bed_min"] == 465.0
    assert row["sws_min"] == 90.0
    assert row["rem_min"] == 105.0

    # asleep is derived from stages, not in-bed minus awake
    assert row["asleep_min"] == 405.0          # 210 + 90 + 105
    assert row["sleep_need_total_min"] == 510.0  # 450 + 40 + 20 + 0

    assert row["sleep_fulfilment_pct"] == 79.4   # 405 / 510
    assert row["sws_pct_of_sleep"] == 22.2       # 90 / 405
    assert row["rem_pct_of_sleep"] == 25.9       # 105 / 405
    assert row["disturbances_per_hour"] == 1.33  # 9 / 6.75h


def test_unscored_records_produce_blanks_not_zeros():
    """A PENDING_SCORE cycle must not land in the CSV as strain 0."""
    rows = build_daily_rows([PENDING_CYCLE], [], [], [])
    row = _only(rows, "2026-09-24")
    assert row["day_strain"] is None
    assert row["recovery_score"] is None
    assert row["cycle_id"] == 93846  # the day still exists, it's just unscored


def test_missing_sleep_does_not_break_the_row():
    rows = build_daily_rows([CYCLE], [RECOVERY], [], [WORKOUT])
    row = _only(rows, "2026-09-23")
    assert row["sleep_id"] is None
    assert row["asleep_min"] is None
    assert row["day_strain"] == 14.87  # everything else survived


def test_naps_do_not_replace_the_main_sleep():
    nap = dict(SLEEP, id="nap-0001", nap=True)
    nap["score"] = dict(SLEEP["score"])
    # Recovery points at the real sleep, so the nap must lose.
    rows = build_daily_rows([CYCLE], [RECOVERY], [nap, SLEEP], [])
    assert _only(rows, "2026-09-23")["sleep_id"] == SLEEP["id"]


def test_workout_zone_durations_flatten_to_minutes():
    flat = flatten_workout(WORKOUT)
    assert flat["date"] == "2026-09-23"
    assert flat["duration_min"] == 30.1
    assert flat["zone4_min"] == 12.0
    assert flat["zone5_min"] == 1.0
    assert flat["distance_meter"] == 5378.1


def test_workout_rows_sorted_and_date_filtered():
    later = dict(WORKOUT, id="w2", start="2026-09-23T18:00:00.000Z",
                 end="2026-09-23T18:30:00.000Z")
    undated = dict(WORKOUT, id="w3", start=None, end=None)
    rows = build_workout_rows([later, WORKOUT, undated])
    assert [r["workout_id"] for r in rows] == [WORKOUT["id"], "w2"]


def test_merge_upserts_rather_than_duplicating():
    """Re-syncing a window must overwrite, and a late score must win."""
    existing = [{"date": "2026-09-22", "recovery_score": "61"},
                {"date": "2026-09-23", "recovery_score": ""}]
    new = [{"date": "2026-09-23", "recovery_score": 44.0},
           {"date": "2026-09-24", "recovery_score": 70.0}]

    merged = merge_rows(existing, new, key="date")
    assert [r["date"] for r in merged] == ["2026-09-22", "2026-09-23", "2026-09-24"]
    assert merged[1]["recovery_score"] == 44.0   # blank overwritten by real score
    assert merged[0]["recovery_score"] == "61"   # untouched day preserved


def test_rows_are_ordered_by_date():
    day_two = dict(CYCLE, id=93847, start="2026-09-25T13:00:00.000Z")
    rows = build_daily_rows([day_two, CYCLE], [], [], [])
    assert [r["date"] for r in rows] == ["2026-09-23", "2026-09-25"]


def test_cycle_near_local_midnight_lands_on_the_day_it_covers():
    """
    Real regression. For a late-night sleeper WHOOP's cycle boundary lands
    either side of local midnight, so keying on the cycle START puts two
    cycles on one date (one overwriting the other) and leaves the next date
    with no row. These are real boundaries from 2026-09.
    """
    off = "-04:00"
    # 09-02 02:02 -> 09-02 23:56 local. Genuinely Sept 2.
    assert cycle_date("2026-09-02T06:02:00Z", "2026-09-03T03:56:00Z", off) == "2026-09-02"
    # 09-02 23:56 -> 09-04 04:11 local. Starts on the 2nd, but it IS Sept 3.
    assert cycle_date("2026-09-03T03:56:00Z", "2026-09-04T08:11:00Z", off) == "2026-09-03"
    # The pair above collided under the old start-keyed scheme.
    assert local_date("2026-09-02T06:02:00Z", off) == local_date(
        "2026-09-03T03:56:00Z", off
    )


def test_open_cycle_assumes_a_24_hour_span():
    # Today's cycle has no end yet; it must still land on today, not yesterday.
    assert cycle_date("2026-10-04T02:41:19Z", None, "-04:00") == "2026-10-04"


def test_whole_month_of_cycles_yields_one_row_per_day():
    off = "-04:00"
    boundaries = [
        ("2026-09-19T08:28:00Z", "2026-09-20T02:27:00Z"),
        ("2026-09-20T02:27:00Z", "2026-09-21T04:14:00Z"),
        ("2026-09-21T04:14:00Z", "2026-09-22T02:29:00Z"),
        ("2026-09-22T02:29:00Z", "2026-09-23T03:22:00Z"),
    ]
    dates = [cycle_date(s, e, off) for s, e in boundaries]
    assert dates == ["2026-09-19", "2026-09-20", "2026-09-21", "2026-09-22"]
    assert len(set(dates)) == len(dates), "no two cycles may share a date"


def test_merge_drops_a_row_whose_cycle_moved_to_another_date():
    """
    Upsert-on-key never deletes, so re-keying a cycle leaves the old row
    behind forever. This is the real one: cycle 1406489727 appeared on both
    2026-04-01 (old start-keyed) and 2026-04-02 (new midpoint-keyed).
    """
    stale = {"date": "2026-04-01", "cycle_id": "1406489727", "day_strain": "12.48"}
    fresh = {"date": "2026-04-02", "cycle_id": "1406489727", "day_strain": "12.48"}
    out = merge_rows([stale], [fresh], key="date", identity="cycle_id")
    assert [r["date"] for r in out] == ["2026-04-02"]

    # An untouched older row with its own cycle must NOT be swept away.
    other = {"date": "2026-03-30", "cycle_id": "1400000000", "day_strain": "9.0"}
    out = merge_rows([other, stale], [fresh], key="date", identity="cycle_id")
    assert [r["date"] for r in out] == ["2026-03-30", "2026-04-02"]

    # Without identity the old behaviour is unchanged.
    out = merge_rows([stale], [fresh], key="date")
    assert len(out) == 2


def test_recovery_records_survive_the_raw_archive():
    """
    WHOOP v2 recovery records carry no `id` — only `cycle_id`. Deduping the
    raw archive on `id` alone discarded every one of them silently.
    """
    # Imported here, not at module scope: store.py pulls in the Google client
    # libraries, and this suite is meant to run on a bare checkout.
    try:
        from src.store import DriveStore
    except ImportError:
        print("        (skipped: google client libs not installed)", end="")
        return

    recovery = {"cycle_id": 93845, "sleep_id": "abc", "score": {"recovery_score": 61}}
    assert DriveStore._identity(recovery) is not None
    assert DriveStore._identity({"id": 7}) == "id:7"
    assert DriveStore._identity({"score": {}}) is None
    # Two recoveries for different cycles must not collapse into one.
    other = dict(recovery, cycle_id=93846)
    assert DriveStore._identity(recovery) != DriveStore._identity(other)


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
