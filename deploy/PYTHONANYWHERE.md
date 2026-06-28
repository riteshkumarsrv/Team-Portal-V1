# PythonAnywhere (free tier)

Host Team Portal V1 from [GitHub](https://github.com/riteshkumarsrv/Team-Portal-V1).

## 1. Account

Sign up at [pythonanywhere.com](https://www.pythonanywhere.com) (free tier). Note your **username** — your site will be `https://USERNAME.pythonanywhere.com`.

## 2. Bash console (one-time setup)

Open **Consoles → Bash** and run:

```bash
curl -sL https://raw.githubusercontent.com/riteshkumarsrv/Team-Portal-V1/main/scripts/pythonanywhere_bootstrap.sh | bash
```

Or clone first and run `bash scripts/pythonanywhere_bootstrap.sh`.

Edit `~/Team-Portal-V1/.env`:

- `MANAGER_DASHBOARD_PASSWORD` — manager login password
- `PUBLIC_URL=https://USERNAME.pythonanywhere.com`
- `TEAM_TRACKER_PRODUCTION=1` (should already be set)

## 3. Web app

**Web** tab → **Add a new web app** → **Manual configuration** → **Python 3.10**.

| Setting | Value |
|---------|--------|
| Source code | `/home/USERNAME/Team-Portal-V1` |
| Virtualenv | `/home/USERNAME/.virtualenvs/team-portal-v1` |
| Static files | URL `/static/` → `/home/USERNAME/Team-Portal-V1/static/` |

**WSGI configuration file:** replace contents with `deploy/pythonanywhere_wsgi.py` from the repo, changing `YOUR_USERNAME` to your PA username.

Click **Reload** (green button).

## 4. Verify

- `https://USERNAME.pythonanywhere.com/healthz` → `{"status":"ok"}`
- `https://USERNAME.pythonanywhere.com/login` → login page

## Optional: API deploy

After bootstrap, from your PC (with [API token](https://www.pythonanywhere.com/account/#api_token)):

```powershell
$env:PA_USERNAME='your_username'
$env:PA_API_TOKEN='your_token'
python scripts/pythonanywhere_deploy_api.py
```
