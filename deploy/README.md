# Deploying to AWS

Puts the whole app (reels **and** VO tightening) on one small EC2 box behind
HTTPS and a password, so you can send one link to someone.

## What you need first

1. **AWS CLI installed and configured**

   ```
   aws configure
   ```

   It asks for an access key, secret, and region — use `ap-south-1` (Mumbai).
   Create the key under IAM → Users → Security credentials.

2. **A `.env` in the repo root** with real values:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ELEVENLABS_API_KEY=...
   ```

   It is uploaded over SSH after boot — never committed, never in user-data.

3. The repo pushed to GitHub. The server clones it on first boot.
   If the repo is **private**, either make it public or bake a deploy token into
   `REPO_URL` before running.

## Deploy

```
bash deploy/provision.sh
```

Takes ~5 minutes. It prints the URL, username and a generated password at the
end. **Save the password — it is not stored anywhere.**

### Swapping the password for a one-click link

If you would rather share a link than a password, replace the `basic_auth`
block in `/etc/caddy/Caddyfile` with a cookie gate. Visiting `/k/<token>` once
sets a year-long cookie; every later request carries it, and anything without
it gets a bare 404 (so the host is not advertised to scanners that mine
Certificate Transparency logs). See the deployed Caddyfile for the exact shape —
note the unlock route must `respond` with an HTML meta-refresh rather than
`redir`, which loses to Caddy's directive ordering and returns 200.

You get:

| | |
|---|---|
| `https://<ip>.nip.io/` | reels |
| `https://<ip>.nip.io/tighten` | VO tightening |

`nip.io` resolves any `<ip>.nip.io` to that IP, so Caddy gets a real Let's
Encrypt certificate without you buying a domain.

## Running costs

| item | approx |
|---|---|
| t3.small (2 GB) | $15/mo |
| 20 GB disk | $1.60/mo |
| elastic IP (while attached) | free |
| **total** | **~$18/mo** |

Not AWS: ElevenLabs ≈ 7¢ per 10-min video, Claude review ≈ 15¢ per video.

**Stop the box when it's idle** — you're only billed for the disk (~$1.60/mo):

```
aws --region ap-south-1 ec2 stop-instances  --instance-ids <id>
aws --region ap-south-1 ec2 start-instances --instance-ids <id>
```

The IP is static, so the link keeps working after a restart.

Set a budget alarm so $100 can't quietly disappear:
Billing → Budgets → create a $50 monthly alert.

## Operating it

```
ssh -i ~/.ssh/clipeditor.pem ubuntu@<ip>
journalctl -u clipeditor -f          # app logs
sudo systemctl restart clipeditor    # restart app
```

**Deploying a code change:**

```
ssh ... 'cd /opt/clipeditor && git pull && sudo systemctl restart clipeditor'
```

**Changing the password (or the link token):** edit `/etc/caddy/Caddyfile` —
for basic auth replace the hash with the output of
`caddy hash-password --plaintext 'newpass'`; for the cookie gate change the
token in both the `@unlocked` matcher and the `/k/<token>` route. Then
`sudo systemctl restart caddy`. Changing either revokes access for everyone.

Uploaded media and finished cuts are deleted after 7 days by a daily cron.

## Tear it down

```
bash deploy/destroy.sh <instance-id> <allocation-id> <sg-id>
```

(the three IDs are printed by `provision.sh`)

## Notes

- The app listens on `127.0.0.1` only. Everything public goes through Caddy,
  which enforces the password — there is no unauthenticated path to it.
- SSH is locked to the IP of the machine that ran `provision.sh`. If your home
  IP changes, update the rule in the EC2 console → Security Groups.
- One gunicorn worker on purpose: jobs run in background threads and ffmpeg
  already saturates a small box.
