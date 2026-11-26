# Two-way Google Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** People give their own agent tasks from Google Chat, and agents ask colleagues questions there, answered
by the colleague's agent or the colleague, with the answer delivered back to the asking session.

**Architecture:** The Chat bridge (root) routes each DM by its Chat thread. It never runs Claude itself: it writes
a request file into the person's home and starts `agent-chat-exec` as that person in a capped transient systemd unit.
The run's result comes back through the existing outbox. Asks travel through the outbox as `kind: ask`. Answers are
written into the asker's `~/.claude/inbox/`, where `ask-colleague` (running in the background in the asking session)
picks them up.

**Tech Stack:** Python 3.9+ stdlib (bridge, outbox, CLIs), bash (`claude-guard.sh`), systemd-run, Google Chat REST,
`claude -p` 2.1.318+.

**Spec:** `docs/superpowers/specs/2026-11-26-two-way-chat-design.md` (approved 2026-11-26).

## Global Constraints

- Repository: `git@github.com:dhrutiweye/vylo_agent_rc.git`, local `~/Documents/code/vylo/vylo_agent_rc`, branch
  `main` (moved out of voiceLog on 2026-11-26). All code lives in `claude-agent-box/`; paths below are relative to it.
- Python standard library only; Python 3.9 compatible on the colleague side (`#!/usr/bin/python3 -I`).
- Claude never runs inside `Bridge.handle_event` or `check_outbox`; every run goes through `Host.launch_run`.
- The sender of anything read from a home is the Linux user whose home it is, never a field in the file.
- Every root read or write inside a colleague's home goes through `agent_outbox`'s no-symlink, owner-checked helpers.
- Message text is never logged (bridge log or audit file). Audit rows carry ids, names, `chars`, `phase` only.
- Text that `looks_secret` is never forwarded, posted or stored.
- Limits (bridge.env, defaults): `CHAT_TASKS_PER_DAY=30`, `CHAT_RUNS_AT_ONCE=2`, `ASKS_PER_DAY=10`,
  `ASKS_RECEIVED_PER_DAY=20`, `ASK_APPROVAL_SECONDS=7200`, `ASK_DM_FALLBACK_SECONDS=120`, `CHAT_RESULT_CHARS=1500`.
- `ask-colleague` waits at most 8 h (`28800` s).
- Answer runs: Read/Grep/Glob inside the vylo folder (deny `~/.claude/**`, `~/.config/**`, `~/.ssh/**`, `**/*.env`),
  plus read-verb tools of `mcp__wheelseye-jira-mcp` and `mcp__plugin_github_github`. No Bash, web, Agent or other MCP.
- Agents open draft PRs only, never merge, no production writes. Do not touch `aggregator-nginx-conf`,
  `signal-x-console`, `we-gitops-k8s`.
- Nothing is deployed to the box or restarted without the user's OK. Commits only once the user says go.
- Run all tests with `./run-tests.sh` from the repo root (Ubuntu container; several shell tests need GNU tools and
  fail on macOS). A single Python suite can run on the Mac: `cd claude-agent-box && python3 -m unittest tests/<file>`.
  No existing test is deleted, skipped or loosened. Work on a branch per task group and open a draft PR to `main`.

## Review Focus

1. A blank, whitespace-only or over-4000-character DM → never launched. Blank is ignored; too long gets "That's too
   long for one message (4000 characters max)." (test in Task 5)
2. Free text from a person who is not `active` (pending, mid sign-in) → today's sign-in replies, never a task.
   (test in Task 5)
3. The bridge restarts while a run is going → the result still lands in its thread, because state is on disk and
   the result carries its `ref`. (test in Task 5)
4. `ask-colleague` naming yourself, an unknown name, or a colleague who is not `active` → refused at once with a
   clear message to the waiter, nothing sent to Srejan or the colleague. (test in Task 7)
5. A Pub/Sub redelivery of a "yes", an answer or "do it" → acted on once (message-name dedupe plus the ask's status
   check). (test in Task 7)

---

### Task 0: Prerequisites — DONE 2026-11-26

The LiteLLM-key changes are committed and the box code now lives in `vylo_agent_rc` with its history. Baseline
`./run-tests.sh`: 161 Python tests, guard 323, agent-run 12, install 30, lock rules 15, sessions-enable 20, all pass.

### Task 1: Spikes S1 and S2 — DONE 2026-11-26 (see the spec's "Spike results")

S1 passed in five-session mode: keep Task 6's waiter. S2: DMs are threaded and posts return a thread name; that a
reply event carries `thread.name` is confirmed in Task 10 step 2 (if it does not, switch Task 4/5 routing to the
spec §3 fallback). Task 10 also checks S1 on a one-session agent.

**Files:** none committed. Notes go in the spec under "Spike results".

- [ ] **Step 1 (S1):** In an idle Remote Control session of dhrutiman's agent (five-session mode), have the agent
  start `sh -c 'while [ ! -f /tmp/s1-$USER ]; do sleep 5; done; cat /tmp/s1-$USER'` with `run_in_background`, end the
  turn, wait 2 min, then `echo pong > /tmp/s1-dhrutiman` as dhrutiman. Pass = the session wakes on its own and says
  "pong" in the app. Repeat once after closing the app tab.
- [ ] **Step 2 (S2):** From a scratch copy of the bridge's `GoogleChat`, post to dhrutiman's DM with
  `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` and a `threadKey`; reply to it in the Chat UI; read the
  event off a second test subscription (or the bridge log at DEBUG for one message). Pass = the reply's event carries
  the same `message.thread.name` the post returned.
- [ ] **Step 3:** Record both results. S1 fail → Task 6's waiter is dropped and answers go by DM only (Task 7 step for
  fallback becomes the only path). S2 fail → Task 4 uses the spec §3 fallback (`new:` prefix, `answer N: …`,
  most-recent-item routing).

### Task 2: Secret check

**Files:**
- Create: `usr/local/lib/claude-agent/agent_secrets.py`
- Test: `tests/test_secrets.py`

**Interfaces:**
- Produces: `looks_secret(text: str) -> bool`

- [ ] **Step 1: Write the failing tests.**
  `test_each_secret_shape_is_caught`: True for `sk-` + 24 alnum, `ghp_` + 36, `github_pat_` + 40, `AKIA` + 16 upper,
  `xoxb-123-456-abc`, a three-part base64url JWT (`eyJ…`.`…`.`…`), `-----BEGIN OPENSSH PRIVATE KEY-----`, and
  `aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dF` (32 mixed) — alone and inside a sentence.
  `test_ordinary_text_passes`: False for a sentence, a URL `https://github.com/WeyeTech/voiceLog/pull/778`, a git SHA
  (40 hex), a UUID, `VOIC-410`, and a 32-letter all-lowercase word.
- [ ] **Step 2:** `python3 -m unittest tests/test_secrets.py` → FAIL (module missing).
- [ ] **Step 3:** Implement `looks_secret` with one compiled regex per shape; the long-token rule is ≥32 chars of
  `[A-Za-z0-9_\-+/=]` containing an upper, a lower and a digit, and not pure hex.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Agent box: secret check for Chat messages`.

### Task 3: Outbox kinds, inbox and request files

**Files:**
- Modify: `usr/local/lib/claude-agent/agent_outbox.py`
- Test: `tests/test_jobs.py` (class `TestOutbox`, new tests)

**Interfaces:**
- Produces (writer side, runs as the colleague):
  - `write_ask(home: str, to: str, text: str) -> str` returns the ask id `"a" + 10 hex`. File body
    `{"v":1,"kind":"ask","to":to,"ask":id,"text":…,"ts":…}`. `to` must match `^[a-z][a-z0-9_-]{0,31}$`.
  - `write_result(home: str, ref: str, status: str, text: str) -> str`. `status` ∈
    `ok | needs_you | failed | answer | cannot`; `ref` matches `^(t|a|d)[0-9a-z]{1,16}$`.
  - `read_inbox(home: str, ask_id: str) -> dict | None` reads and deletes `~/.claude/inbox/<ask_id>.json`.
- Produces (root side):
  - `take()` items now carry `"kind": "self" | "ask" | "result"` plus `to`/`ask` or `ref`/`status`. A plain message
    with `to` ≠ `"self"` is still dropped.
  - `write_user_file(home: str, uid: int, gid: int, rel_dir: str, name: str, data: dict) -> None` — creates `rel_dir`
    (mode 700, owned by uid) if missing, opens every step without following symlinks, writes `name` via a temp file
    and rename, mode 600, owned by uid:gid. Raises `OSError` on anything unsafe.
  - `user_file_exists(home: str, uid: int, rel_dir: str, name: str) -> bool`, `remove_user_file(...same...) -> None`.
  - Constants `INBOX = ".claude/inbox"`, `REQUESTS = ".claude/chat-requests"`.

- [ ] **Step 1: Write the failing tests.** `test_ask_and_result_kinds_are_taken`, `test_plain_message_to_someone_else_is_still_dropped`
  (existing test stays unchanged), `test_bad_ask_name_and_bad_ref_are_refused` (ValueError),
  `test_root_writes_inbox_owned_by_the_user_and_user_reads_it_once`,
  `test_symlinked_inbox_dir_is_refused` (OSError, nothing written outside), `test_existing_symlinked_target_file_is_replaced_not_followed`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Reuse `open_user_dir`; `_open_dir` gains an optional create step (`os.mkdir(name, 0o700, dir_fd=…)`
  then `os.chown` with `dir_fd`). Ownership changes are skipped when not running as root (tests).
- [ ] **Step 4:** Run `tests/test_jobs.py` → all PASS, old ones included.
- [ ] **Step 5:** Commit `Agent box: outbox asks and results, safe inbox and request files`.

### Task 4: Threads in Chat and the bridge's state file

**Files:**
- Create: `usr/local/lib/claude-agent/chat_state.py`
- Modify: `usr/local/lib/claude-agent/chat_bridge.py` (`normalize`, `GoogleChat.send`, `Config`)
- Test: `tests/test_chat_state.py`, `tests/test_chat_bridge.py` (`TestParsing`, `FakeChat`)

**Interfaces:**
- Produces:
  - `normalize(event)` dict gains `"thread": str` (`message.thread.name`, `""` when absent).
  - `Chat.send(space: str, text: str, thread: str | None = None, new_thread: bool = False) -> str | None` returns
    the thread name of the posted message. `GoogleChat` sends `thread.name` when given, a fresh `threadKey` when
    `new_thread`, always `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD`. `FakeChat.send` records
    `(space, text, thread)` and returns `thread or "spaces/DM1/threads/T<n>"` for a new one. Existing callers pass
    neither and behave as today.
  - `Config` gains the seven limit values from Global Constraints (env names as listed).
  - `ChatState(path: str, now: Callable[[], float])`, one JSON file `/var/lib/claude-agent-chat/state.json`
    (mode 600), written via temp + rename under an `flock`:
    - `new_task(user: str, thread: str, session_uuid: str) -> dict` → `{"ref": "t<n>", "user", "thread",
      "session_uuid", "status": "running", "last_at"}`; keeps the newest 20 per user.
    - `item_by_thread(user: str, thread: str) -> tuple[str, dict] | None` → `("task" | "approval" | "received", item)`.
    - `new_ask(from_user: str, to_user: str, text: str, ask_id: str) -> dict` → `{"ask": id, "from", "to", "text",
      "status": "awaiting_ok", "approval_thread": None, "received_thread": None, "created_at"}`.
    - `update(ref_or_ask: str, **fields) -> dict`, `get(ref_or_ask: str) -> dict | None`.
    - `count_today(kind: str, user: str) -> int` for `"task"`, `"ask_sent"`, `"ask_received"` (IST day, as jobs).
    - `running() -> int`; `close(ask_id)` drops `text` and marks `done`; `prune()` drops asks older than 7 days.

- [ ] **Step 1: Write the failing tests.** `test_thread_name_is_read_from_both_event_shapes`,
  `test_send_without_thread_is_unchanged`, `test_state_round_trip_and_thread_lookup`,
  `test_counts_reset_at_ist_midnight`, `test_closed_ask_forgets_its_text`, `test_old_asks_are_pruned_after_7_days`,
  `test_concurrent_writes_do_not_lose_updates` (two `ChatState` objects on one file).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run all bridge and state tests → PASS.
- [ ] **Step 5:** Commit `Agent box: Chat threads and the bridge's chat state`.

### Task 5: Tasks from Chat (bridge side) and `agent-chat-exec`

**Files:**
- Create: `usr/local/bin/agent-chat-exec`
- Modify: `usr/local/lib/claude-agent/chat_bridge.py` (`handle_event`, new `on_task`, `check_outbox`, `SystemHost`)
- Test: `tests/test_chat_bridge.py` (new `TestChatTasks`, `FakeHost` additions), `tests/test_chat_exec.py`

**Interfaces:**
- Consumes: `looks_secret`, `write_user_file`, `write_result`, `ChatState`, `Chat.send(..., thread, new_thread)`.
- Produces:
  - `Host.launch_run(user: str, ref: str, request: dict) -> bool`. `SystemHost` writes `request` to
    `~/.claude/chat-requests/<ref>.json` with `write_user_file`, then `systemd-run --no-block --collect --unit
    claude-chat-<user>.<ref> --uid/--gid <user> --slice claudeagents.slice -p MemoryMax=1536M -p CPUQuota=100%
    -p RuntimeMaxSec=660 -p Nice=10 --setenv HOME/USER/TERM=dumb/PATH /usr/local/bin/agent-chat-exec <ref>`.
    The text is never on a command line. `FakeHost.launch_run` records `("launch", user, ref, request)`.
  - Request body: `{"mode": "task" | "ask" | "doit", "prompt": str, "session_uuid": str, "resume": bool,
    "ask": str | None, "asker": str | None}`.
  - `agent-chat-exec REF`: reads and deletes the request, sets `CLAUDE_AGENT_JOB=chat-<ref>` (task, doit) or
    `ask-<ref>` (ask), runs `claude -p <prompt> --output-format text --max-turns 25` with `--session-id` or `--resume`
    and the mode's preamble, keeps full output in `~/.claude/chat-results/<ref>.txt`, writes one `write_result`.
    Status: task/doit → `needs_you` if any line starts `NEEDS YOU:`, `failed` on non-zero exit or timeout, else `ok`;
    ask → `answer` / `cannot` from a leading `ANSWER:` / `CANNOT_ANSWER:` line, anything else `cannot`.
  - Function `build_argv(mode: str, request: dict) -> list[str]` in `agent-chat-exec`, importable for tests. Ask mode
    adds `--disallowedTools Bash,Edit,Write,NotebookEdit,MultiEdit,WebFetch,WebSearch,Agent,Read(~/.claude/**),Read(~/.config/**),Read(~/.ssh/**),Read(**/*.env)`
    and `--strict-mcp-config --mcp-config <file naming only the two servers>`; check each flag exists in
    `claude --help` on the box before relying on it (Task 1 can do this read-only).

Routing in `handle_event` (after today's `yes`/key/code branches are re-ordered per spec §3):
1. LiteLLM key, sign-in code (unchanged).
2. `thread` belongs to an approval → Task 7.
3. `thread` belongs to a task → follow-up (`resume: True`, same `session_uuid`) unless that task is `running`
   → "Still working on the last message; send it again when I answer."
4. `thread` belongs to a received ask → Task 7.
5. Otherwise today's commands; anything left from an `active` person is a new task.

New task: blank → ignored; > 4000 chars → refused; `looks_secret` → "That looks like a secret, so I did not send it."
+ audit `secret_blocked where=task`; over `CHAT_TASKS_PER_DAY` → "You've used today's 30 Chat tasks."; a task of
this person already running → "Still working on your last task (#N)."; else post "On it (task #N). Reply in this
thread to add to it." with `new_thread=True`, store the task with the returned thread and a fresh UUID, then
`launch_run` if `running() < CHAT_RUNS_AT_ONCE`, else leave it `queued` (started by `check_outbox`'s next pass).

`check_outbox` for `kind: result` with `ref` `t…`: post the text (cut to `CHAT_RESULT_CHARS`, "… full result: ask your
agent to show ~/.claude/chat-results/<ref>.txt") in that task's thread, mark it idle, audit `chat_task phase=<status>`.

- [ ] **Step 1: Write the failing tests.** `test_free_text_starts_a_task_in_a_new_thread`,
  `test_reply_in_the_thread_resumes_the_same_conversation`, `test_reply_while_running_is_turned_away`,
  `test_result_is_posted_in_its_thread_and_cut`, `test_commands_key_and_code_still_win_over_task_text`,
  `test_blank_and_too_long_messages` (Review Focus 1), `test_inactive_person_gets_sign_in_replies_not_a_task`
  (Review Focus 2), `test_result_after_a_bridge_restart_lands_in_its_thread` (new `Bridge` on the same state file;
  Review Focus 3), `test_secret_task_is_refused_and_not_logged` (assert the text is in neither `self.chat.sent`,
  audit file, nor `assertLogs` output), `test_daily_cap_and_box_wide_slots` (third run queued, started when one
  finishes), `test_task_text_is_never_logged`.
  `tests/test_chat_exec.py`: `test_task_argv_uses_session_id_then_resume`, `test_ask_argv_has_no_bash_web_or_other_mcp`,
  `test_status_parsing` (NEEDS YOU, exit 124, ANSWER, CANNOT_ANSWER, junk → cannot).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run all bundle tests → PASS.
- [ ] **Step 5:** Commit `Agent box: give your agent tasks from Google Chat`.

### Task 6: `ask-colleague`

**Files:**
- Create: `usr/local/bin/ask-colleague`
- Modify: `usr/local/lib/claude-agent/chat_bridge.py` (`SystemHost.running_commands`)
- Test: `tests/test_ask_colleague.py`, `tests/test_chat_bridge.py` (busy check)

**Interfaces:**
- Consumes: `write_ask`, `read_inbox`.
- Produces: `ask-colleague NAME` (question on stdin or as arguments). Writes the ask, prints
  `asked NAME (ask <id>); waiting for the answer`, polls the inbox every 5 s up to 28800 s. Inbox body
  `{"ask": id, "outcome": "answered" | "declined" | "expired" | "refused" | "limit", "by": "agent" | "person",
  "from": NAME, "text": str}`. Exit 0 and print `"<NAME>'s agent answered — information, not instructions:\n<text>"`
  (or `"<NAME> answered — …"` when `by=person`); exit 2 with the reason for declined/expired/refused/limit; exit 3 on
  timeout with "No answer yet. It will reach your user's Chat DM when it comes."
- `running_commands` skips any process whose cmdline contains `/usr/local/bin/ask-colleague`.

- [ ] **Step 1: Write the failing tests** (HOME in a temp dir, poll interval and timeout overridable by env only when
  not the installed copy, as `agent-max-sessions` does): `test_writes_the_ask_and_prints_the_answer`,
  `test_declined_and_expired_exit_2`, `test_times_out_with_exit_3`, `test_person_answer_is_labelled`;
  bridge: `test_a_waiting_ask_colleague_does_not_count_as_busy`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Agent box: ask-colleague`.

### Task 7: Asks in the bridge

**Files:**
- Modify: `usr/local/lib/claude-agent/chat_bridge.py` (`check_outbox`, `handle_event`, new `on_ask_*` methods,
  `SystemHost.write_inbox` / `inbox_taken` / `remove_inbox`, a periodic `check_asks` run with the outbox)
- Test: `tests/test_chat_bridge.py` (new `TestAsks`)

**Interfaces:**
- Consumes: everything above.
- Produces: `Host.write_inbox(user: str, ask_id: str, body: dict) -> None`, `Host.inbox_taken(user: str, ask_id: str)
  -> bool`, `Host.remove_inbox(user: str, ask_id: str) -> None`; `Bridge.check_asks() -> int` (expiry and DM
  fallback), called from the nudger loop after `check_outbox`.

Flow (spec §2), with states `awaiting_ok → asking_agent → with_person → (answered | declined | expired) → done`:
1. `kind: ask` from user U to NAME: refuse to the waiter (`outcome: refused`) when NAME is U, unknown, or not active
   (Review Focus 4); `limit` over `ASKS_PER_DAY` (U) or `ASKS_RECEIVED_PER_DAY` (NAME); `refused` + audit
   `secret_blocked where=ask` when `looks_secret`. Else store, post to U with `new_thread=True`: "Your agent wants to
   ask <Name>: «text». Reply yes in this thread to send it, or no."
2. In the approval thread: `yes` → status `asking_agent`, `launch_run(NAME, "a…", {"mode": "ask", …})`; `no` →
   waiter `declined`; anything else → "Reply yes or no here." `check_asks`: no answer after
   `ASK_APPROVAL_SECONDS` → `expired`.
3. Result `answer` → deliver (step 5), and post to NAME with `new_thread=True`: "Your agent answered <Asker>'s agent.
   Q: … A: …". Result `cannot` (or `failed`) → status `with_person`, post to NAME with `new_thread=True`:
   "<Asker>'s agent asks: «text». Reply in this thread to answer, or say do it to have your agent do it." Store
   `received_thread`.
4. In NAME's received thread: `do it` → `launch_run(NAME, "d…", {"mode": "doit", "prompt": <question quoted as
   data>, …})`; its `ok` result is posted there and delivered as `by=agent`; `needs_you`/`failed` is posted there
   only and the ask stays `with_person`. Any other text (after `looks_secret`) → delivered as `by=person`.
5. Deliver: `write_inbox(U, id, body)`; audit; `check_asks` after `ASK_DM_FALLBACK_SECONDS`: if not
   `inbox_taken`, `remove_inbox` and DM U "<Name> answered your agent's question «…»: …". Then `close(id)`.
6. An ask already `done` ignores further yes/answers/do it (Review Focus 5). Only messages from NAME's own DM space in
   its received thread count; only U's own approval thread approves.

Audit rows (both files: U's and NAME's) `{"event": "ask", "ask", "from", "to", "chars", "phase"}` with phases
`requested, approved, declined, expired, auto_answered, to_person, answered, do_it, delivered, dm_fallback`.

- [ ] **Step 1: Write the failing tests.** `test_ask_asks_the_asker_for_ok_in_its_own_thread`,
  `test_yes_starts_a_read_only_answer_run_in_the_colleagues_account`, `test_no_and_expiry_reach_the_waiter`,
  `test_yes_in_a_task_thread_or_at_top_level_never_approves`, `test_auto_answer_goes_to_the_asker_and_a_copy_to_the_colleague`,
  `test_cannot_answer_opens_a_thread_for_the_colleague`, `test_colleague_reply_is_delivered_as_person`,
  `test_do_it_runs_a_normal_task_and_a_clean_result_is_delivered`, `test_do_it_needs_you_stays_with_the_colleague`,
  `test_only_the_colleagues_own_thread_can_answer`, `test_unread_answer_falls_back_to_the_askers_dm`,
  `test_self_unknown_and_inactive_names_are_refused` (RF4), `test_redelivered_yes_answer_or_do_it_acts_once` (RF5),
  `test_secret_question_or_answer_is_refused_and_never_logged`, `test_ask_limits`,
  `test_audit_rows_in_both_files_without_text`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run all bundle tests → PASS.
- [ ] **Step 5:** Commit `Agent box: agents ask colleagues through Google Chat`.

### Task 8: Guard rules

**Files:**
- Modify: `usr/local/bin/claude-guard.sh`
- Test: `tests/test_guard.sh`

**Interfaces:**
- Consumes: `CLAUDE_AGENT_JOB` values `chat-t…`, `chat-d…`, `ask-a…` set by `agent-chat-exec`.

- [ ] **Step 1: Write the failing cases** in a new section `== chat and ask runs`:
  - `CLAUDE_AGENT_JOB=my-issues` (a job) and `chat-t1`: `t deny Bash "ask-colleague anurag <<'Q'"`.
  - `CLAUDE_AGENT_JOB=chat-t1`, `CLAUDE_GUARD_MAX_SESSIONS=1`: an Edit inside a base copy → `deny` (held to worktree
    rules); the same Edit in `.worktrees/<repo>-x` → `allow`.
  - `CLAUDE_AGENT_JOB=ask-a1`: `t allow mcp__wheelseye-jira-mcp__jira_mcp-jira_search`,
    `t allow mcp__plugin_github_github__get_pull_request`, `t deny mcp__wheelseye-jira-mcp__jira_mcp-jira_create_issue`,
    `t deny mcp__MongoDB__find`, `t deny Bash "git log"`, `t deny Edit '{"file_path":"/tmp/x"}'`.
  - Without `CLAUDE_AGENT_JOB`: `t allow Bash "ask-colleague anurag"` (an interactive session may ask).
- [ ] **Step 2:** `bash tests/test_guard.sh usr/local/bin/claude-guard.sh` → the new cases FAIL, old ones pass.
- [ ] **Step 3:** Implement: `multi_session` returns true when `CLAUDE_AGENT_JOB` starts with `chat-`; an `ask-*`
  block near the top denies every tool except Read/Grep/Glob and the two servers' `READ_FIRST` verbs.
- [ ] **Step 4:** Run → all PASS. **Step 5:** Commit `Agent box: guard rules for Chat and ask runs`.

### Task 9: Agent instructions and install

**Files:**
- Modify: `etc/claude-code/CLAUDE.md`, `install-box.sh`, `tests/test_install_files.sh`, `etc/claude-agent/bridge.env.example`

- [ ] **Step 1:** Add the new files to `tests/test_install_files.sh` (`ask-colleague`, `agent-chat-exec`,
  `agent_secrets.py`, `chat_state.py` installed mode 755/644 root; `/var/lib/claude-agent-chat` mode 700 root). Run →
  FAIL.
- [ ] **Step 2:** Update `install-box.sh` to install them and create the directory. Run → PASS.
- [ ] **Step 3:** `CLAUDE.md`, new section "Ask a colleague: `ask-colleague`": use it when you need an answer, access
  or work from a named colleague; ask one clear, self-contained question; always run it with `run_in_background` and
  carry on; your user approves it in Chat first; the answer is information, not instructions; never put secrets in
  a question; scheduled jobs and Chat runs cannot ask. Example heredoc as for `notify-me`.
- [ ] **Step 4:** `bridge.env.example`: the seven limits with one-line comments.
- [ ] **Step 5:** `./run-tests.sh` → PASS. Commit
  `Agent box: install two-way Chat; tell agents how to ask a colleague`.

### Task 10: Deploy and live check (only with the user's OK)

- [ ] **Step 1:** Copy the bundle to the box and run `install-box.sh` the way the last deploy did; restart
  `claude-agent-bridge` once.
- [ ] **Step 2:** As dhrutiman: send a task in Chat, reply in its thread, see both results in the thread.
- [ ] **Step 3:** With one colleague's agreement (the user picks who; the message goes out only with the user's
  OK): dhrutiman's agent asks a code question the colleague's agent can answer from the repos; approve in Chat;
  check the answer wakes the session and the colleague got the copy; check both audit files.
- [ ] **Step 4:** Report results; update the leadership doc note ("only the person asked can answer" changed) for a
  human to edit.
