# Vylo coding-agent platform: fresh EC2 box

One box, one Linux user per colleague, each running their own Claude agent (their own Claude account). They join
and sign in through Google Chat; they never log in to the box. Policy: reads are free; production writes, merges
and protected-branch pushes are denied; PRs are drafts only. Nothing in this folder contains a key, a project
name or an address of your current test setup: you enter those on the box.

## Box

- **Ubuntu 24.04** or **Amazon Linux 2023** (x86_64 or arm64). `ec2-setup.sh` detects which and installs accordingly. 4 vCPU / 16 GB RAM for 3 to 5 colleagues (8 GB is the floor). 100 GB gp3 disk.
- Outbound only is enough: no inbound ports except SSH from your admin network. The box needs internet (GitHub,
  Google, npm, Anthropic) and a route to the LiteLLM gateway. No IAM role is needed.
- Anyone who can run commands as root here can read the shared Jira/LiteLLM values: keep SSH access to admins.

## 1. Copy the package to the box (from your laptop)

Through a bastion, one command copies straight to the box (nothing is staged on the bastion). Replace the bastion user,
key, box address and login (`ec2-user` on Amazon Linux, `ubuntu` on Ubuntu) with yours:

```bash
J=<bastion-user>@<bastion-host>; K=~/.ssh/<your-key>; BOX=ec2-user@<box-ip>
scp -J $J -i $K claude-agent-ec2.zip $BOX:~
# the Chat service-account key (the file from Google Cloud). Never email or chat it, and do not leave a copy on the bastion:
scp -J $J -i $K vylo-agent-bot-key.json $BOX:~
ssh -J $J -i $K $BOX
```

If the bastion refuses forwarding ("administratively prohibited"), copy to the bastion first, then from the bastion to
the box, and delete both copies from the bastion afterwards.

Check the file arrived intact:

```bash
sha256sum claude-agent-ec2.zip        # compare with claude-agent-ec2.zip.sha256 from the package
```

## 2. Install (on the box)

```bash
sudo dnf install -y unzip || sudo apt-get install -y unzip      # dnf: Amazon Linux, apt: Ubuntu
unzip claude-agent-ec2.zip && cd claude-agent-ec2
sudo ./ec2-setup.sh
```

Installs the packages and the platform. Starts nothing. Safe to re-run. The person who runs it with `sudo` becomes the box admin.
On Amazon Linux it also lets `sudo` find `/usr/local/bin` (its default does not), so `sudo add-colleague.sh` works as written.

## 3. Configure

```bash
sudo ./configure-box.sh          # Google project and subscription, the Chat key, the shared Jira kit (LiteLLM key + Jira url)
sudo ./github-bot-login.sh       # the READ-ONLY mirror login (hidden prompt). Root keeps it; agents never get it
sudo vylo-mirror-sync.sh         # mirror the repos from GitHub with that login
sudo ./verify-box.sh --online    # everything should say PASS (the bridge may say WARN: not started yet)
```

`configure-box.sh` asks for the Jira site url and a LiteLLM key limited to the `jira_mcp` server (hidden prompt). Those two are
the only shared Jira values. Nobody's Jira login is shared: each colleague's own Jira email and API token are collected
when you add them (step 4), so Jira changes carry their name.

## 4. Go live

Only ONE bridge may read the Chat subscription. If another box (for example the Mac test VM) is running the bridge,
stop it first: `sudo systemctl disable --now claude-agent-bridge` there. Two bridges split the messages between them.

```bash
sudo systemctl enable --now claude-agent-bridge
sudo add-colleague.sh asha asha@yourcompany.com      # name: a-z 0-9 _ -, 2 to 20 chars; the email they use in Chat
# it asks for Asha's OWN GitHub token and her OWN Jira email + API token (hidden), checks both, and only then creates the account
# (--no-jira creates a user without Jira)
```

If a check fails (a repo the token cannot reach, or Jira rejecting the email and token), it says so and **nothing is created**. Fix the token and run
the same command again. Two colleagues cannot use the same GitHub user.

Tell Asha: open Google Chat, find **Vylo Agent**, send **hi**, open the link, sign in with your own Claude account,
paste the code back. Signing in turns on Remote Control for their account; their agent `asha-agent` then appears in
the Claude app. They get one session. The bot nudges them in Chat when the agent waits on them: after 5 minutes,
5 minutes later, then every 30 minutes. Nothing is auto-denied; the agent just waits.

## Doorbell, reminders and sessions

- **Doorbell.** Every agent can post to its own user's Chat DM with `notify-me` (never to anyone else's), for things
  only that person can do: review, deploy, approve in the Claude app, message a colleague. At most 20 an hour.
- **Scheduled jobs.** A colleague asks their agent, for example "remind me weekdays at 9:45 about standup" or "every
  weekday at 10, list my open VOIC issues and tell me what to do first". The agent runs `agent-job`. A reminder posts
  text; a task runs a fresh headless Claude on that person's own plan and posts the result. Limits, enforced by
  `claude-agent-jobs.service` (edit `/etc/claude-code/agent-jobs.env`, then restart it): 10 jobs a person, reminders
  at most every 15 minutes, tasks at most hourly, 24 task runs a day, 10 minutes and 1.5 GB per run, one task per
  person and two on the box at a time, a job pauses after 3 failures in a row. A task cannot ask for approval; what
  would need it is refused and reported as "NEEDS YOU".
- **Sessions.** Each person has one live session. In Chat: `new session [NAME]` starts a fresh conversation and keeps
  the old one, `name NAME` names the one you are in, `sessions` lists the last 5, `switch N` or `switch NAME` goes
  back, `switch LINK` moves the agent onto a session link copied from the Claude app, `forget N` / `forget all`
  tidies the list. A session without a name is shown by the first thing typed in it
  (read from its transcript; a `/rename` title wins). `forget` only edits the bridge's list: the session still shows
  in the Claude app until it is archived there. `jobs` and `job pause|resume|remove|run NAME` manage jobs.
  Instructions for the agents are in `/etc/claude-code/CLAUDE.md`.
  `new session` and `switch` restart the agent, which ends whatever it is doing. If it is mid-request, waiting for an
  approval, or still running a command (background ones included), the bot says so and waits up to 2 minutes for a
  "yes" before going ahead.
- **Several sessions (up to 5 a person).** Off by default: `/etc/claude-code/agent-sessions.conf` says `default=1`.
  `sudo sessions-enable <name>` turns one person up to 5 after checking that every repo copy in their vylo folder is
  clean and on its default branch, and that their Claude org policy allows merging with the box's settings (WheelsEye
  accounts need `"managedSourcesBehavior": "merge"` added by a claude.ai Owner first; until then they stay at 1
  whatever the file says). Their agent then runs `claude remote-control --capacity N --spawn same-dir`: they open,
  switch and close sessions in the Claude app. In Chat, `sessions` lists their recent conversations and which are
  active, `restart` restarts the agent (sessions open less than about 4 hours come back if Claude can restore them
  within the limit; the limit wins), and `new session`/`switch`/`name`/`forget` point to the app. The repo copies are
  shared by all their sessions, so the guard refuses edits, commits and branch switches there: every change happens
  in a worktree (`vylo/.worktrees/<repo>-<branch>`). At most 2 helpers a session. Nudges and the busy check name the
  session that is waiting. `sudo sessions-enable --off <name>` puts them back to 1.
- **Colleagues on an org plan.** If someone's Claude org pushes a policy without `"managedSourcesBehavior": "merge"`,
  Claude skips `/etc/claude-code/managed-settings.json` for them: no guard, no audit log, no several sessions.
  `sudo lock-box-rules <name>` copies the box's rules into their own `~/.claude/settings.json`, owned by root and
  immutable, which Claude does run; their agent picks it up when it restarts. `install-box.sh` keeps those copies in
  step with the box rules (`--refresh-all`); `--check` and `--unlock` do what they say. `verify-box.sh` warns for anyone
  whose guard does not run. (If the org ever sets `allowManagedHooksOnly`, this stops working and they fall back to 1.)
- **Box load.** `claude-agent-monitor.timer` samples memory, disk and each person's sessions every 5 minutes into
  `/var/log/claude-agent-load.jsonl`, and alerts `ADMIN_PERSON` (in `bridge.env`) in Chat as `[box]`: memory under
  1.5 GB twice in a row, disk under 10 GB, or an out-of-memory kill, each at most once an hour. All agents and jobs
  share `claudeagents.slice`, capped at 12 GB; one person at 4 GB. An agent joins the slice when it restarts: after
  installing this, restart each running agent once (`sudo systemctl restart claude-agent@<name>`) when its person is idle.

## Day to day

| Do | Command |
|---|---|
| Who is on the box | `sudo jq -r '.people[]|[.name,.email,.status]|@tsv' /etc/claude-agent/people.json` |
| Usage and adoption report | `sudo claude-agent-report --days 7` (add `--json`; ends with the Load section: peak sessions, memory, OOM kills) |
| Let a person run several sessions | `sudo sessions-enable <name>` (checks their repo copies first; `--off <name>` to undo) |
| Box load right now / its log | `sudo claude-agent-monitor --print` / `/var/log/claude-agent-load.jsonl` |
| Remove a colleague | `sudo remove-colleague.sh <name>` (shows what it would do), then `--yes <name>` |
| Bridge log | `sudo journalctl -u claude-agent-bridge -f` |
| One person's agent | `sudo systemctl status claude-agent@<name>` / `restart` |
| Change a person's CPU/RAM caps | `sudo systemctl edit claude-agent@<name>` (defaults: 4 GB, 1.5 CPU; all agents together 12 GB in `claudeagents.slice`) |
| Replace a colleague's GitHub token (expired, rotated) | `sudo set-gh-token.sh <name>` (checks it first; keeps the old one if the check fails) |
| Replace a colleague's Jira email/token | `sudo set-jira-token.sh <name>` (checks it against Jira first) |
| Check a GitHub token without installing it | `printf '%s' "$TOKEN" \| sudo gh-token-check.sh` |
| Refresh the repo mirrors now | `sudo vylo-mirror-sync.sh` (also runs daily at 02:30) |
| Re-run the checks | `sudo ./verify-box.sh --online` |
| Scheduled jobs runner log | `sudo journalctl -u claude-agent-jobs -f` |
| One person's jobs | `sudo -u <name> agent-job list` |
| Change the job limits | `/etc/claude-code/agent-jobs.env`, then `sudo systemctl restart claude-agent-jobs` |
| Change the nudge timing | `/etc/claude-agent/bridge.env`, then `sudo systemctl restart claude-agent-bridge` |

Audit logs (metadata only, no prompt text) are in `/var/log/claude-agent/<name>.jsonl`.

## Not covered

- Docker builds run outside the per-user CPU/RAM caps, so one person's image build can use the whole box.
- Colleagues' own Claude plans set their usage limits. Nothing here caps spend.
- PRs, pushes and Jira changes carry each colleague's own GitHub and Jira user. The LiteLLM key is shared by everyone. When it
  changes, run `sudo set-litellm-key.sh` (hidden prompt; `--host` if the gateway moved too). It checks the key against the
  gateway first, then rewrites it in the starter kit AND every agent home. Re-running `configure-box.sh` only rebuilds the
  kit, so existing colleagues would keep the old key. Open sessions pick the new key up on their next session or `/mcp` reconnect.
- Claude's Remote Control needs each colleague's Claude account to be on a plan that includes it.

## GitHub tokens (fine-grained)

There are two kinds, and only the first ever reaches an agent.

**1. Each colleague's own token** (the agent pushes branches and opens draft PRs as that person on GitHub). The colleague
creates it at GitHub > Settings > Developer settings > Fine-grained tokens. Resource owner: the org. Repositories: only the
six in `data/repos.txt`. Permissions: Metadata read, **Contents read+write**, **Pull requests read+write**. Leave
Workflows and everything else off. If the org requires approval for fine-grained tokens, an owner must approve each one
before it works. Hand it to the admin over a channel that is not Chat or email (for example, they type it at the admin's
hidden prompt). The same goes for the Jira API token (id.atlassian.com > Security > API tokens). `add-colleague.sh` and `set-gh-token.sh` check read, push, PR read and PR create on every repo before
anything is installed; the token is stored only in that colleague's home (`~/.config/gh/hosts.yml`, mode 600).
`people.json` records the GitHub login and the expiry date (not the token), so you can see who is due to renew.

**2. The mirror login** (`github-bot-login.sh`): one read-only token kept by root, used only by `vylo-mirror-sync.sh`.
It needs Metadata read and Contents read, nothing more. Agents never receive it. When it expires, run
`sudo ./github-bot-login.sh` again.

Keep branch protection on master/main/release*: the guard also blocks those pushes and merges, but a server-side rule is
what holds if the guard ever fails.
