# Slack Scorer Bot — Setup Guide

The bot listens for submission messages in any channel it belongs to, clones
the submitted GitHub repo, checks it against the Stage-2 rubric, and DMs the
student a full score report.

---

## 1. Create the Slack App

1. Go to **https://api.slack.com/apps** → **Create New App** → **From scratch**
2. Name it something like `Stage2 Scorer` and pick your workspace.

### Enable Socket Mode (no public URL needed)
- **Settings → Socket Mode** → toggle **Enable Socket Mode: ON**
- Click **Generate** to create an App-Level Token
  - Name it anything (e.g. `scorer-token`)
  - Scope: `connections:write`
  - Copy the token — it starts with `xapp-`

### Add Bot Token Scopes
- **Features → OAuth & Permissions → Scopes → Bot Token Scopes**
- Add these scopes:

| Scope | Why |
|---|---|
| `chat:write` | Post messages in channels |
| `im:write` | Open and write DMs |
| `channels:history` | Read messages in public channels |
| `groups:history` | Read messages in private channels |
| `im:history` | Read DMs (to detect submissions there too) |
| `users:read` | Look up user info |

### Subscribe to Events
- **Features → Event Subscriptions** → toggle **Enable Events: ON**
- Under **Subscribe to bot events**, add:
  - `message.channels`
  - `message.groups`
  - `message.im`

### Install the App
- **Settings → Install App** → **Install to Workspace**
- Copy the **Bot User OAuth Token** — it starts with `xoxb-`

---

## 2. Run the Bot Locally

```bash
# From the repo root
cd slack_bot

# Install dependencies
pip install -r requirements.txt

# Copy and fill in your tokens
cp .env.example .env
# Edit .env and paste your xoxb- and xapp- tokens

# Start the bot
python bot.py
```

You should see:
```
⚡ Stage-2 Scorer Bot is running — waiting for submissions…
```

---

## 3. Invite the Bot to a Channel

In Slack: `/invite @Stage2 Scorer` (or whatever you named it) to the channel
where students will submit.

---

## 4. Submit a Repo for Scoring

Any message in that channel matching:
```
submit https://github.com/student-username/their-repo
```

The bot will:
1. Acknowledge publicly in the channel
2. Clone the repo (must be **public**)
3. Run all rubric checks (~10–30 seconds)
4. DM the student the full report

### Example DM output

```
📊 Stage-2 Submission Score Report
🔗 Repo: https://github.com/student/stage-2-test

Total: 74/98  (76%)  —  Grade: C 😐

━━━━ Section 1 — Containerisation  (38/46) ━━━━

API Dockerfile
  ✅ Multi-stage build (+3)
  ✅ Named non-root USER (+3)
  ✅ HEALTHCHECK instruction (+2)
  ✅ No .env files COPY'd in (+2)
  ✅ Slim / Alpine base image (+1)
  ❌ User created via adduser/useradd (+1)
...
```

---

## 5. Scoring Rubric Summary

| Area | Max pts |
|---|---|
| API Dockerfile | 12 |
| Frontend Dockerfile | 12 |
| docker-compose.yml | 22 |
| Lint stage | 8 |
| Test stage | 9 |
| Build stage | 8 |
| Security Scan stage | 8 |
| Integration Test stage | 8 |
| Deploy stage | 6 |
| Pipeline Hygiene | 5 |
| **Total** | **98** |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Bot doesn't respond | Make sure it's invited to the channel (`/invite @BotName`) |
| "Could not clone repository" | The repo must be **public** |
| Scoring times out | GitHub may be slow; try again |
| DM not received | Check `im:write` scope is added and app reinstalled |
