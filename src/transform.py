"""
Flatten WHOOP records into analysis-ready rows.

The raw JSON is kept verbatim elsewhere; this module exists so that trend
questions ("has slow-wave sleep been falling for three days?") can be answered
by reading one CSV instead of 180 JSON files.

Two decisions worth knowing:

* A WHOOP "cycle" is a physiological day — it starts when you wake, not at
  midnight. Rows are keyed by the LOCAL date the cycle started, using the
  timezone_offset WHOOP reports, so a row lines up with how the day actually
  felt rather than with UTC.

* Derived fields are computed here rather than left to the reader, because the
  interesting signals are ratios, not raw values. Sleep debt only means
  something against sleep need; REM only means something as a share of actual
  sleep.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

MS_PER_MIN = 60_000.0

DAILY_COLUMNS = [
    "date",
    # cycle / strain
    "cycle_id", "cycle_start", "cycle_end", "day_strain",
    "cycle_avg_hr", "cycle_max_hr", "cycle_kilojoule",
    # recovery
    "recovery_score", "hrv_rmssd_milli", "resting_heart_rate",
    "spo2_percentage", "skin_temp_celsius", "user_calibrating",
    # sleep, as reported
    "sleep_id", "sleep_start", "sleep_end", "is_nap",
    "sleep_performance_pct", "sleep_consistency_pct", "sleep_efficiency_pct",
    "respiratory_rate", "sleep_cycle_count", "disturbance_count",
    # sleep stages, minutes
    "in_bed_min", "awake_min", "light_min", "sws_min", "rem_min", "no_data_min",
    # sleep need, minutes
    "need_baseline_min", "need_from_debt_min", "need_from_strain_min",
    "need_from_nap_min",
    # derived
    "asleep_min", "sleep_need_total_min", "sleep_fulfilment_pct",
    "sws_pct_of_sleep", "rem_pct_of_sleep", "disturbances_per_hour",
    # workouts rolled up
    "workout_count", "workout_strain_sum", "workout_min", "workout_sports",
]

WORKOUT_COLUMNS = [
    "date", "workout_id", "sport_name", "start", "end", "duration_min",
    "strain", "average_heart_rate", "max_heart_rate", "kilojoule",
    "percent_recorded", "distance_meter", "altitude_gain_meter",
    "zone0_min", "zone1_min", "zone2_min", "zone3_min", "zone4_min", "zone5_min",
]

_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def local_date(ts: str | None, offset: str | None) -> str | None:
    """
    Convert a UTC timestamp plus WHOOP's timezone_offset ('-04:00') into the
    local calendar date. Falls back to the UTC date if the offset is missing
    or malformed, which is better than dropping the record.
    """
    if not ts:
        return None
    dt = _parse(ts)
    if dt is None:
        return None
    m = _OFFSET_RE.match((offset or "").strip())
    if m:
        sign = 1 if m.group(1) == "+" else -1
        dt = dt + sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return dt.date().isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _min(ms) -> float | None:
    """Milliseconds to minutes, rounded to 1dp. None-safe."""
    if ms is None:
        return None
    try:
        return round(float(ms) / MS_PER_MIN, 1)
    except (TypeError, ValueError):
        return None


def _pct(numerator, denominator) -> float | None:
    if not numerator or not denominator:
        return None
    try:
        return round(100.0 * float(numerator) / float(denominator), 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _scored(record: dict) -> dict:
    """
    Return the score object, or {} when WHOOP hasn't scored the record.

    score_state is SCORED / PENDING_SCORE / UNSCORABLE. Treating an unscored
    record as zeros would silently poison every trend, so it becomes blanks.
    """
    if record.get("score_state") != "SCORED":
        return {}
    return record.get("score") or {}


def flatten_workout(w: dict) -> dict:
    score = _scored(w)
    zones = score.get("zone_durations") or score.get("zone_duration") or {}
    start, end = _parse(w.get("start")), _parse(w.get("end"))
    duration = round((end - start).total_seconds() / 60.0, 1) if start and end else None

    return {
        "date": local_date(w.get("start"), w.get("timezone_offset")),
        "workout_id": w.get("id"),
        "sport_name": w.get("sport_name"),
        "start": w.get("start"),
        "end": w.get("end"),
        "duration_min": duration,
        "strain": score.get("strain"),
        "average_heart_rate": score.get("average_heart_rate"),
        "max_heart_rate": score.get("max_heart_rate"),
        "kilojoule": score.get("kilojoule"),
        "percent_recorded": score.get("percent_recorded"),
        "distance_meter": score.get("distance_meter"),
        "altitude_gain_meter": score.get("altitude_gain_meter"),
        "zone0_min": _min(zones.get("zone_zero_milli")),
        "zone1_min": _min(zones.get("zone_one_milli")),
        "zone2_min": _min(zones.get("zone_two_milli")),
        "zone3_min": _min(zones.get("zone_three_milli")),
        "zone4_min": _min(zones.get("zone_four_milli")),
        "zone5_min": _min(zones.get("zone_five_milli")),
    }


def _sleep_fields(s: dict) -> dict:
    score = _scored(s)
    stages = score.get("stage_summary") or {}
    need = score.get("sleep_needed") or {}

    in_bed = _min(stages.get("total_in_bed_time_milli"))
    awake = _min(stages.get("total_awake_time_milli"))
    light = _min(stages.get("total_light_sleep_time_milli"))
    sws = _min(stages.get("total_slow_wave_sleep_time_milli"))
    rem = _min(stages.get("total_rem_sleep_time_milli"))
    no_data = _min(stages.get("total_no_data_time_milli"))

    # Actual sleep, derived from stages rather than trusting in-bed time.
    asleep = None
    if None not in (light, sws, rem):
        asleep = round(light + sws + rem, 1)

    need_parts = [
        _min(need.get("baseline_milli")),
        _min(need.get("need_from_sleep_debt_milli")),
        _min(need.get("need_from_recent_strain_milli")),
        _min(need.get("need_from_recent_nap_milli")),
    ]
    need_total = round(sum(p for p in need_parts if p is not None), 1) if any(
        p is not None for p in need_parts
    ) else None

    disturbances = stages.get("disturbance_count")
    per_hour = None
    if disturbances is not None and asleep:
        per_hour = round(float(disturbances) / (asleep / 60.0), 2)

    return {
        "sleep_id": s.get("id"),
        "sleep_start": s.get("start"),
        "sleep_end": s.get("end"),
        "is_nap": s.get("nap"),
        "sleep_performance_pct": score.get("sleep_performance_percentage"),
        "sleep_consistency_pct": score.get("sleep_consistency_percentage"),
        "sleep_efficiency_pct": score.get("sleep_efficiency_percentage"),
        "respiratory_rate": score.get("respiratory_rate"),
        "sleep_cycle_count": stages.get("sleep_cycle_count"),
        "disturbance_count": disturbances,
        "in_bed_min": in_bed,
        "awake_min": awake,
        "light_min": light,
        "sws_min": sws,
        "rem_min": rem,
        "no_data_min": no_data,
        "need_baseline_min": need_parts[0],
        "need_from_debt_min": need_parts[1],
        "need_from_strain_min": need_parts[2],
        "need_from_nap_min": need_parts[3],
        "asleep_min": asleep,
        "sleep_need_total_min": need_total,
        "sleep_fulfilment_pct": _pct(asleep, need_total),
        "sws_pct_of_sleep": _pct(sws, asleep),
        "rem_pct_of_sleep": _pct(rem, asleep),
        "disturbances_per_hour": per_hour,
    }


def build_daily_rows(
    cycles: list[dict],
    recoveries: list[dict],
    sleeps: list[dict],
    workouts: list[dict],
) -> list[dict]:
    """
    Join the four collections into one row per physiological day.

    Cycles are the spine. Recovery joins on cycle_id; sleep joins via the
    recovery's sleep_id (falling back to cycle_id on the sleep record);
    workouts are rolled up by their own local date.
    """
    recovery_by_cycle = {r.get("cycle_id"): r for r in recoveries if r.get("cycle_id")}
    sleep_by_id = {s.get("id"): s for s in sleeps if s.get("id")}

    # Naps are excluded from the main sleep join — the night's sleep is what a
    # cycle's recovery is scored from — but their need contribution still shows
    # up in need_from_recent_nap_milli.
    sleep_by_cycle = {
        s.get("cycle_id"): s
        for s in sleeps
        if s.get("cycle_id") and not s.get("nap")
    }

    workouts_by_date: dict[str, list[dict]] = {}
    for w in workouts:
        flat = flatten_workout(w)
        if flat["date"]:
            workouts_by_date.setdefault(flat["date"], []).append(flat)

    rows = []
    for c in sorted(cycles, key=lambda x: x.get("start") or ""):
        offset = c.get("timezone_offset")
        date = local_date(c.get("start"), offset)
        if not date:
            continue

        cycle_score = _scored(c)
        row = {k: None for k in DAILY_COLUMNS}
        row.update(
            {
                "date": date,
                "cycle_id": c.get("id"),
                "cycle_start": c.get("start"),
                "cycle_end": c.get("end"),
                "day_strain": cycle_score.get("strain"),
                "cycle_avg_hr": cycle_score.get("average_heart_rate"),
                "cycle_max_hr": cycle_score.get("max_heart_rate"),
                "cycle_kilojoule": cycle_score.get("kilojoule"),
            }
        )

        rec = recovery_by_cycle.get(c.get("id"))
        if rec:
            rscore = _scored(rec)
            row.update(
                {
                    "recovery_score": rscore.get("recovery_score"),
                    "hrv_rmssd_milli": rscore.get("hrv_rmssd_milli"),
                    "resting_heart_rate": rscore.get("resting_heart_rate"),
                    "spo2_percentage": rscore.get("spo2_percentage"),
                    "skin_temp_celsius": rscore.get("skin_temp_celsius"),
                    "user_calibrating": rscore.get("user_calibrating"),
                }
            )

        sleep = None
        if rec and rec.get("sleep_id"):
            sleep = sleep_by_id.get(rec["sleep_id"])
        if sleep is None:
            sleep = sleep_by_cycle.get(c.get("id"))
        if sleep is not None:
            row.update(_sleep_fields(sleep))

        day_workouts = workouts_by_date.get(date, [])
        strains = [w["strain"] for w in day_workouts if w["strain"] is not None]
        mins = [w["duration_min"] for w in day_workouts if w["duration_min"] is not None]
        row.update(
            {
                "workout_count": len(day_workouts),
                "workout_strain_sum": round(sum(strains), 2) if strains else None,
                "workout_min": round(sum(mins), 1) if mins else None,
                "workout_sports": "; ".join(
                    sorted({w["sport_name"] for w in day_workouts if w["sport_name"]})
                )
                or None,
            }
        )

        rows.append(row)

    return rows


def build_workout_rows(workouts: list[dict]) -> list[dict]:
    rows = [flatten_workout(w) for w in workouts]
    return sorted(
        [r for r in rows if r["date"]],
        key=lambda r: (r["date"], r["start"] or ""),
    )


def merge_rows(existing: list[dict], new: list[dict], key: str) -> list[dict]:
    """
    Upsert `new` over `existing` on `key`, newest wins, sorted by key.

    Re-syncing a window must not duplicate rows, and late-scored records
    (WHOOP scores recovery only once a sleep closes) must be able to overwrite
    an earlier blank.
    """
    merged = {r[key]: r for r in existing if r.get(key)}
    for r in new:
        if r.get(key):
            merged[r[key]] = r
    return [merged[k] for k in sorted(merged)]
