# Pushing this team portal to Gerrit

Run **every** `git` command from the repository root:

```text
C:\Users\z0017fzc\Projects\SCRUM_vF
```

That is the only path you need (`cd` there in PowerShell, or open that folder in your IDE terminal) before `git add`, `git commit`, `git push`, or Gerrit review pushes.

## 1. Remote: Nokia Gerrit (`origin`)

`origin` is set to:

`https://bhgerrit.ext.net.nokia.com/a/BTS_TRS/WebTools/TeamScrumBoard.git`

To point `origin` elsewhere (or fix the URL):

```powershell
cd C:\Users\z0017fzc\Projects\SCRUM_vF
git remote set-url origin "https://bhgerrit.ext.net.nokia.com/a/BTS_TRS/WebTools/TeamScrumBoard.git"
git remote -v
```

To keep **GitHub** and use Gerrit as a second name (example):

```powershell
git remote rename origin github
git remote add origin "https://bhgerrit.ext.net.nokia.com/a/BTS_TRS/WebTools/TeamScrumBoard.git"
```

## 2. Push for code review (typical Gerrit flow)

Many Gerrit servers expect a push to `refs/for/<branch>` (e.g. `main`):

```powershell
git push origin HEAD:refs/for/main
```

Some teams use `master` or a topic:

```powershell
git push origin HEAD:refs/for/main%topic=my-feature
```

Use whatever branch name and ref your **Gerrit project documentation** specifies.

## 3. Direct push (only if your project allows non-review pushes)

Rare on strict Gerrit setups; only if you have rights and the project is configured for it:

```powershell
git push origin main:main
```

## 4. Before you push

- Do **not** commit `.env`, live `*.db`, or large SQL dumps (see root `.gitignore`).
- Commit from `C:\Users\z0017fzc\Projects\SCRUM_vF` so paths and hooks stay consistent.

## Other “Team Portal” repo (FastAPI scaffold only)

If you instead meant the small **FastAPI** scaffold under `TeamPortal`, use:

```text
C:\Users\z0017fzc\Projects\TeamPortal
```

That tree is a separate git repository and is **not** the Flask manager/leave/Scrum site on port 5000.
