# workbuddy-checkin

> A tiny, self-contained Python script that auto-claims your **WorkBuddy** daily check-in credits. Zero dependencies, zero bundled secrets — just run it on a machine where you've logged in to the WorkBuddy desktop client.

WorkBuddy (Tencent's AI coding companion) hands out free daily credits through a check-in. This script reverse-engineers the exact endpoints the desktop client uses, reads **your own local login session**, and claims the daily reward for you — idempotently, every day.

## Features
- **Pure standard library** — no `pip install`, runs on any Python 3.
- **Single file**, ~290 lines, fully self-contained.
- **Idempotent & safe** — checks status first, claims only when needed; re-running never double-claims.
- **Smart one-line report** — e.g. `成功领取 100 积分（连续 7 天，累计 700 积分）` / `今日已签过（...）` / `签到活动未开启` / `登录态已失效`.
- **Robust** — handles both "already checked in" response shapes (`null` and `400 + code 10001`), detects 401/403 session expiry, recognizes off-season (`active=false`).
- **Cross-platform** — auto-detects the WorkBuddy credential file on Windows / macOS / Linux.
- **No secrets in the repo** — reads only the runner's own local credentials; safe to fork and share.

## How it works
After you log in, the WorkBuddy desktop client writes a plain-JSON session file (`workbuddy-desktop.info`) containing an `accessToken`. The script:
1. Locates that file (platform auto-detect, or `WORKBUDDY_AUTH_FILE` override).
2. `POST /v2/billing/meter/checkin-activity-status` — is today's reward already claimed?
3. If not, `POST /v2/billing/meter/daily-checkin` — claim it.
4. Prints one JSON line with a human-readable `report`.

All requests go to the same endpoint the official client uses (`https://copilot.tencent.com`).

## Prerequisites
1. **WorkBuddy desktop client** installed and logged in once (this writes the credential file).
2. **Python 3** on PATH (any version; no third-party packages).

## Quick start
```bash
git clone https://github.com/88lin/workbuddy-checkin.git
cd workbuddy-checkin
python checkin.py auto
```
If you see `今日已签过` or `成功领取 N 积分`, it works.

## Usage
```
python checkin.py auto     # daily automation: check → claim if needed → one-line report
python checkin.py status   # just query status (debug)
python checkin.py claim    # just claim (debug, idempotent)
python checkin.py all      # query status + claim (debug)
```
Output is always a single JSON line; the `report` field is the human-readable summary (in Chinese, matching the official client).

## Automate it (daily at 00:05)
Recommended: a **WorkBuddy scheduled automation**. The script needs your *local* desktop credentials, so cloud CI (e.g. GitHub Actions) won't work.

1. Place `checkin.py` somewhere stable, e.g. `<workspace>/.workbuddy/automations/daily-checkin/checkin.py`.
2. Create a WorkBuddy automation:
   - **Name**: 每日自动领 WorkBuddy 积分
   - **Schedule**: daily at 00:05
   - **Prompt**:
     ```
     运行 python "<absolute path to checkin.py>" auto，
     把命令输出的 JSON 里 report 字段的内容，直接一句话汇报给我。
     若 report 含"领取失败"或"登录态已失效"，提醒我重新登录 WorkBuddy 桌面端。
     ```

## Configuration
| Env var | Purpose |
|---|---|
| `WORKBUDDY_AUTH_FILE` | Override the credential file path if auto-detection fails. |

## Troubleshooting
| Symptom | Fix |
|---|---|
| `NO_AUTH / 未找到登录凭据` | Log in to the WorkBuddy desktop client once; or set `WORKBUDDY_AUTH_FILE`. |
| `NO_SESSION / HTTP 401\|403` | Session expired — re-login to the desktop client; automation resumes automatically. |
| `INACTIVE / 签到活动未开启` | Off-season, no check-in activity running. Normal, no action needed. |
| Debug raw API responses | `python checkin.py status` or `python checkin.py all`. |

## Security & privacy
- Reads **only your own local** WorkBuddy session file. Contains, embeds, and transmits no third-party secrets.
- No `accessToken` is ever printed; the `Authorization` header is never logged.
- Safe to fork, share, and run on your own machine — it only ever acts on *your* login session.

## Disclaimer
This project is **unofficial** and not affiliated with Tencent or WorkBuddy. The check-in endpoints were reverse-engineered from the desktop client's `app.asar` for personal automation. Use at your own risk; endpoints may change without notice. Please respect the service's terms of use.

## License
[MIT](LICENSE) © 2026 88lin
