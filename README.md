# whoop-pipeline

A scheduled ETL job that pulls the full WHOOP v2 record set every morning and
lands it in Google Drive — verbatim JSON for fidelity, flat CSV for trend
analysis.

Built because the three numbers WHOOP shows you on the app home screen are the
*conclusion*, not the evidence. "Recovery 42%" is not actionable. "Recovery 42%
because slow-wave sleep dropped 35% against baseline and respiratory rate is up
0.8" is.

---

## What it collects

| Source | Endpoint | What comes out |
|---|---|---|
| Cycles | `v2/cycle` | day strain, average and max HR, kilojoules |
| Recovery | `v2/recovery` | recovery score, HRV (RMSSD), resting HR, SpO₂, skin temperature |
| Sleep | `v2/activity/sleep` | stage durations, sleep need breakdown, respiratory rate, efficiency, consistency, disturbances, cycle count |
| Workouts | `v2/activity/workout` | per-activity strain, HR zone durations, distance, elevation |
| User | `v2/user/profile/basic`, `v2/user/measurement/body` | height, weight, max HR |

---

## Architecture

```
GitHub Actions (cron, 11:00 UTC / 7am ET)
        │
        ├── src/whoop.py      OAuth2 + rotation-safe token handling, cursor pagination
        ├── src/transform.py  join 4 collections into one row per physiological day
        └── src/store.py      Google Drive read/modify/write
                │
                ▼
        Google Drive folder
                ├── _state.json        sync cursor + the rotating WHOOP token
                ├── daily.csv          44 columns, one row per day
                ├── workouts.csv       19 columns, one row per workout
                └── raw/YYYY-MM.json   untouched API records
```

Drive is the sink because it is the only store both the job and an LLM
assistant can reach. Everything downstream reads `daily.csv` for trends and
drops into `raw/` when a question needs a field the CSV doesn't carry.

---

## The two problems worth reading the code for

### 1. Refresh tokens rotate

WHOOP invalidates your refresh token every time you use it and issues a
replacement. A process that crashes between "refresh succeeded" and "new token
saved" locks the account out permanently and needs a manual browser
reauthorisation.

Handled in `whoop.py` by persisting through a callback *before* the new token
is adopted in memory, so a crash leaves storage holding a spent token (already
broken, no worse) rather than losing the only live one. The workflow also sets
a `concurrency` group so two runs can never race on the same token.

The token cannot live in a GitHub secret, because CI would need write access
back to its own secrets on every run. It lives in `_state.json` in Drive.
`WHOOP_REFRESH_TOKEN` is a **bootstrap value only** — the first sync spends it
and the pipeline is self-sustaining from then on.

Google's refresh tokens don't rotate, so that one stays a secret.

### 2. A day is not a day

A WHOOP "cycle" starts when you wake, not at midnight. A workout at 11pm and
the sleep that follows belong to the same physiological day but different
calendar dates in UTC.

`transform.py` keys every row on the **local** date the cycle started, derived
from the `timezone_offset` WHOOP reports per record. A row therefore lines up
with how the day actually felt.

### Smaller ones, also handled

- **Unscored records.** `score_state` can be `PENDING_SCORE` or `UNSCORABLE`.
  Writing those as zeros would silently drag every rolling average down, so they
  become empty cells.
- **Late scoring.** Recovery is only computed once a sleep closes, and WHOOP
  revises records after the fact. The job re-pulls a rolling 10-day window and
  upserts, so the store is self-healing rather than append-only.
- **Naps.** Excluded from the nightly-sleep join (they'd overwrite the real
  night) but their contribution still shows up in `need_from_nap_min`.
- **Rate limits.** 100/min and 10,000/day, shared per client ID. 429s back off
  using the `X-RateLimit-Reset` header.

---

## Derived columns

The raw fields are all preserved, but these are computed because the signal is
in the ratio, not the value:

| Column | Meaning |
|---|---|
| `asleep_min` | light + SWS + REM. Derived from stages, not from in-bed minus awake |
| `sleep_need_total_min` | baseline + debt + recent strain + recent nap |
| `sleep_fulfilment_pct` | `asleep_min / sleep_need_total_min` — the actual debt signal |
| `sws_pct_of_sleep` | deep sleep as a share of sleep. Falls first under load |
| `rem_pct_of_sleep` | REM share. Falls first under alcohol and late meals |
| `disturbances_per_hour` | normalises disturbance count against sleep duration |

---

## Setup

Roughly 45 minutes, most of it clicking through consent screens.

### 1. WHOOP developer app

1. Go to <https://developer.whoop.com> and create an app.
2. Redirect URI: `http://localhost:8080/callback` — must match exactly.
3. Enable scopes: `read:profile`, `read:body_measurement`, `read:cycles`,
   `read:recovery`, `read:sleep`, `read:workout`.
4. Save the client ID and secret.

### 2. Google Cloud OAuth client

1. Create a project at <https://console.cloud.google.com>.
2. Enable the **Google Drive API**.
3. Configure the OAuth consent screen, add yourself as a test user, then
   **publish it to "In production"**. If you leave it in Testing, Google expires
   your refresh token after 7 days and the job breaks every week.
4. Create an OAuth client ID of type **Desktop app**. Save the ID and secret.

### 3. Drive folder

Create a folder in your Drive (e.g. `whoop-data`). Copy its ID from the URL —
`drive.google.com/drive/folders/THIS_PART`.

### 4. Mint the refresh tokens, locally

```bash
pip install -r requirements.txt
python scripts/authorize.py whoop
python scripts/authorize.py google
```

Each opens a browser and prints one token. Do not commit them.

### 5. GitHub secrets

Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `WHOOP_CLIENT_ID` | from step 1 |
| `WHOOP_CLIENT_SECRET` | from step 1 |
| `WHOOP_REFRESH_TOKEN` | from step 4 (bootstrap only) |
| `GOOGLE_CLIENT_ID` | from step 2 |
| `GOOGLE_CLIENT_SECRET` | from step 2 |
| `GOOGLE_REFRESH_TOKEN` | from step 4 |
| `DRIVE_FOLDER_ID` | from step 3 |

### 6. First run

Actions → **Sync WHOOP** → Run workflow → tick `backfill`. Pulls 6 months
(`BACKFILL_MONTHS` in the workflow). After that it runs daily at 7am ET.

---

## Local use

```bash
python -m src.sync              # rolling 10-day window
python -m src.sync --days 90
python -m src.sync --backfill
python tests/test_transform.py  # no credentials needed
```

---

## Operations

**"Refresh token rejected."** The stored token was consumed without being
saved, or you re-ran `authorize.py whoop` and invalidated the live one. Re-run
`authorize.py whoop`, update the secret, and delete `whoop_token` from
`_state.json` so the bootstrap path is taken again.

**Google auth fails every 7 days.** The consent screen is still in Testing.
Publish it.

**Rows exist but every score column is blank.** WHOOP hadn't scored them when
the job ran. The next run re-pulls the window and fills them in.

**Nothing in Drive.** Check `DRIVE_FOLDER_ID` is the folder ID, not the full
URL (the code accepts either, but a share link with a query string can confuse
a copy-paste), and that the Google OAuth client was authorised by the same
account that owns the folder.

---

## Notes

- Requires an active WHOOP membership; the API returns nothing without one.
- No GPS data is exposed by the WHOOP API.
- Strain accumulates intraday and only finalises after the cycle closes, which
  is why the job runs post-wake rather than at midnight.
