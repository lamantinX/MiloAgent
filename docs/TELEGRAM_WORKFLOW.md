# Telegram Workflow Guide

This document explains how Telegram works in MiloAgent, covering both the **admin bot** (Bot API) and the **personal user session** (Telethon).

## Architecture Overview

MiloAgent uses two completely separate Telegram integrations:

| Component | Technology | Purpose | Identity |
|-----------|-----------|---------|----------|
| **Admin Bot** | Bot API (`python-telegram-bot`) | Monitoring, alerts, remote control | Bot created via @BotFather |
| **Group Engagement** | Telethon (MTProto) | Scanning groups, replying to messages | Personal Telegram user account |

These are independent — the admin bot never participates in groups, and the user session never receives admin commands.

---

## 1. Admin Bot Setup

### Create a Bot
1. Open Telegram, search for `@BotFather`
2. Send `/newbot`, follow prompts
3. Save the bot token

### Get Your Chat ID
1. Search for `@userinfobot` in Telegram
2. Send `/start`
3. Note your numeric user ID

### Configure
Edit `config/telegram.local.yaml`:
```yaml
bot_token: "123456:ABC-DEF..."
admin_chat_ids:
  - 123456789  # Your numeric Telegram user ID
```

### Admin Bot Commands
| Command | Description |
|---------|-------------|
| `/status` | Current state and action counts |
| `/stats` | Detailed 24h breakdown |
| `/report` | Full daily report |
| `/scan` | Trigger a scan now |
| `/post` | Act on best opportunity |
| `/health` | Account health status |
| `/last N` | Show last N actions |
| `/drafts` | Show pending Telegram reply drafts |
| `/approve ID` | Approve a draft for sending |
| `/reject ID` | Reject a draft |
| `/pause` | Pause all operations |
| `/resume` | Resume operations |
| `/accounts` | List all accounts |

**Security note:** Credentials must never be sent via Telegram chat. Use the web dashboard or CLI to add accounts.

---

## 2. Personal User Session (Telethon)

### Prerequisites
1. Go to https://my.telegram.org
2. Log in with your phone number
3. Go to "API development tools"
4. Create an application (any name)
5. Note the `api_id` and `api_hash`

### Add Account via Dashboard
1. Open the web dashboard
2. Go to Accounts → Add Account → Telegram
3. Enter `api_id` and `api_hash`
4. The account is created as `enabled: false`, `auth_status: not_authorized`

### Add Account via CLI
```bash
python miloagent.py login telegram
```
This starts the QR login flow interactively.

### QR Authorization Flow
1. Dashboard shows a QR code
2. Open Telegram on your phone → Settings → Devices → Link Desktop Device
3. Scan the QR code
4. If 2FA is enabled, enter your Telegram password
5. On success, account becomes `authorized` + `enabled`

### Session Persistence
- The session is saved as a `.session` file in `data/sessions/`
- This file persists across restarts
- **Never commit session files to git** (already in `.gitignore`)
- To revoke: delete the session file and remove the device from Telegram settings

---

## 3. Project Telegram Configuration

Each project can enable Telegram engagement in its YAML:

```yaml
project:
  id: my_product
  business_id: my_business

telegram:
  enabled: true
  action_mode: "approval"  # observe | draft | approval | autonomous
  persona: "helpful_casual"
  target_groups:
    - "auto"           # Scan joined groups
  auto_discover: true
  auto_join: false
  keywords:
    - "charging"
    - "EV"
  max_groups_per_scan: 10
  max_message_age_minutes: 60
  min_relevance_score: 5.0
  max_messages_per_hour: 5
  max_messages_per_day: 20
```

### Action Modes

| Mode | Scans | Generates Reply | Sends Automatically |
|------|-------|----------------|-------------------|
| `observe` | ✅ | ❌ | ❌ |
| `draft` | ✅ | ✅ | ❌ (saved to DB) |
| `approval` | ✅ | ✅ | ❌ (requires `/approve`) |
| `autonomous` | ✅ | ✅ | ✅ (if in allowlist) |

**Default:** `approval` (safe — requires human approval)

### Autonomous Mode Restrictions
When `action_mode: autonomous`, additional limits apply:
- `autonomous_allowed_groups`: explicit allowlist of group IDs
- If allowlist is empty, autonomous sending is blocked
- `min_relevance_score`: minimum score to auto-send (default: 7.0)
- `max_messages_per_hour`: per-account hourly cap
- `max_messages_per_day`: per-account daily cap

---

## 4. Account Routing

Accounts are strictly routed by `business_id` and `assigned_products`:

```
Business A → Product 1 → Telegram Account A1
Business A → Product 2 → Telegram Account A1
Business B → Product 3 → Telegram Account B1
```

- Business A **cannot** use Business B's Telegram account
- A scan for Product 1 only uses accounts assigned to Product 1
- Bot identity accounts are rejected (only personal user accounts work)

---

## 5. Scan → Opportunity → Reply Flow

```
1. Scheduler triggers scan (every 30 min)
2. For each project with telegram.enabled=true:
   a. Get assigned Telegram account (business/product routing)
   b. Check: authorized, not in FloodWait, not on cooldown
   c. Scan joined groups for recent messages
   d. Score messages by keyword match, question signals, freshness
   e. Store opportunities in SQLite
3. For high-scoring opportunities (score >= 5.0):
   a. In approval mode: create a draft in telegram_drafts table
   b. In autonomous mode: generate reply and send (if in allowlist)
4. Admin reviews drafts via /drafts, /approve, /reject
5. Approved drafts are sent on next action cycle
6. Result recorded in actions table
```

---

## 6. Rate Limits and FloodWait

### Per-Account Limits
- `max_messages_per_hour`: default 5
- `max_messages_per_day`: default 20
- Cooldown between messages: 30-120 seconds (human-like delay)

### FloodWait Handling
When Telegram returns a `FloodWaitError`:
1. The wait duration is saved to `telegram_account_state.flood_wait_until`
2. The account enters cooldown
3. No messages are sent until the wait expires
4. The state **persists across restarts** (stored in SQLite)
5. The worker is freed immediately (no blocking sleep)

### Per-Group Limits
- Minimum 30 seconds between messages to the same group
- Rate limit tracked in `telegram_rate_limits` table

---

## 7. Discovery and Warm-Up

### Group Discovery
- `auto_discover: true`: finds candidate groups via Telegram search
- Candidates are saved for review (not auto-joined)
- `auto_join: false` (default): requires manual approval to join

### Warm-Up
A separate job that:
- Reads messages in joined groups
- Marks messages as read
- Reacts to ~15% of messages with emoji
- Does NOT send text replies
- Builds account reputation before engagement

---

## 8. Docker Deployment

### Volumes
```yaml
volumes:
  - ./data:/app/data        # Sessions, DB, cookies
  - ./config:/app/config    # YAML configs
```

The `data/sessions/` directory must persist across container restarts.

### Environment Variables
```yaml
environment:
  - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}  # Admin bot only
  - TZ=${TZ:-UTC}
```

The `TELEGRAM_BOT_TOKEN` env var is for the admin bot only. Personal user credentials are in `config/telegram_user_accounts.local.yaml`.

### File Permissions
Session files should be readable only by the container user:
```bash
chmod 600 data/sessions/*.session
chmod 600 config/*.local.yaml
```

---

## 9. Troubleshooting

### "No Telegram account available"
- Check: account exists in `config/telegram_user_accounts.local.yaml`
- Check: `enabled: true`
- Check: `auth_status: authorized`
- Check: `business_id` and `assigned_products` match the project

### QR Code Not Showing
- Check: `api_id` and `api_hash` are correct
- Check: Network can reach Telegram servers
- Check: No other Telethon session using the same account

### FloodWait
- Wait for the specified duration
- The state persists in DB — restarting won't help
- Check `telegram_account_state` table for `flood_wait_until`

### Messages Not Sending
- Check: account is authorized (`auth_status: authorized`)
- Check: action_mode is not `observe`
- Check: rate limits not exceeded
- Check: group allows the account to write
- Check: opportunity score meets `min_relevance_score`

### Session Expired
- Delete the `.session` file
- Re-authorize via QR login

---

## 10. Credential Rotation

If credentials are compromised:

1. **Revoke API credentials**: Go to https://my.telegram.org → API development tools → Delete the app
2. **Terminate sessions**: Telegram → Settings → Devices → Terminate all other sessions
3. **Delete local session files**: `rm data/sessions/*.session`
4. **Create new API application**: Generate new `api_id` / `api_hash`
5. **Update config**: Edit `config/telegram_user_accounts.local.yaml`
6. **Re-authorize**: Run QR login flow again
7. **Clean git history** (if credentials were committed):
   ```bash
   # WARNING: destructive — coordinate with collaborators
   pip install git-filter-repo
   git filter-repo --path config/telegram_user_accounts.yaml --invert-paths
   git push --force
   ```

---

## 11. Security Checklist

- [ ] `.local.yaml` files are in `.gitignore`
- [ ] `.session` files are in `.gitignore`
- [ ] No credentials in docker-compose.yml environment
- [ ] Admin bot checks numeric user ID (not username)
- [ ] `/addreddit` and `/addtwitter` commands are disabled
- [ ] QR auth enforces TLS for non-loopback connections
- [ ] 2FA password is never logged or stored
- [ ] `api_hash` never appears in browser responses
- [ ] Draft approval re-checks account health before sending
