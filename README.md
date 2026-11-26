# vylo_agent_rc

The Vylo agent box: a shared EC2 server where each colleague has their own Claude Code Remote Control agent,
reached through the Google Chat bot "Vylo Agent".

| Folder | What it is |
|---|---|
| `claude-agent-box/` | Everything installed on the box: the Chat bridge, guard and audit hooks, sessions, scheduled jobs, notify-me, systemd units, and their tests |
| `claude-agent-ec2/` | Creating and configuring the EC2 instance; `verify-box.sh` |
| `docs/superpowers/` | Designs and implementation plans |

Moved out of the voiceLog repository on 2026-11-26 with its history (three commits); it has nothing to do with the
Java services.

## Tests

The box runs Ubuntu, and several shell tests use GNU tools, so run them in a container:

```bash
./run-tests.sh
```
