#!/usr/bin/env bash
# Fresh-box setup for the Vylo coding-agent platform. Supports Ubuntu 24.04 and Amazon Linux 2023 (x86_64 or arm64).
# Idempotent: safe to re-run.
#
#   sudo ./ec2-setup.sh
#
# Installs the OS packages (git, tmux, jq, make, Docker, Node 22, GitHub CLI, Python), then the platform itself
# (claude-agent-box/install-box.sh). It carries no keys, project names or addresses: those are entered with
# ./configure-box.sh. It does not start the Chat bridge or any agent.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:24.04) FAMILY=deb ;;
  ubuntu:*)     FAMILY=deb; echo "warning: tested on Ubuntu 24.04, this is ${VERSION_ID:-?}" >&2 ;;
  amzn:2023)    FAMILY=rpm ;;
  *) echo "unsupported OS: ${PRETTY_NAME:-unknown}. Supported: Ubuntu 24.04, Amazon Linux 2023." >&2; exit 1 ;;
esac
ADMIN=${SUDO_USER:-}; [ -n "$ADMIN" ] && [ "$ADMIN" != root ] || ADMIN=$(id -nu 1000 2>/dev/null || true)
ARCH=$(uname -m)
say() { printf '\n== %s\n' "$*"; }

if [ "$FAMILY" = deb ]; then
  export DEBIAN_FRONTEND=noninteractive
  say "OS packages (apt)"
  apt-get update -q
  apt-get install -y -q git curl unzip tmux jq make tzdata build-essential python3 python3-venv python3-pip ca-certificates gnupg \
    docker.io docker-buildx docker-compose-v2
  say "Node.js 22 (the Jira MCP server runs through npx)"
  if ! command -v node >/dev/null 2>&1 || ! node -v | grep -q '^v22'; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y -q nodejs
  fi
  say "GitHub CLI"
  if ! command -v gh >/dev/null 2>&1; then
    install -d -m 755 /etc/apt/keyrings
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      >/etc/apt/sources.list.d/github-cli.list
    apt-get update -q && apt-get install -y -q gh
  fi
else
  say "OS packages (dnf)"
  # curl is already present as curl-minimal on Amazon Linux 2023; installing "curl" would conflict with it.
  dnf install -y -q git tmux jq make unzip tar gzip findutils which procps-ng util-linux shadow-utils tzdata gcc gcc-c++ \
    ca-certificates python3 python3-pip docker
  # Python 3.11 for the bridge's virtualenv (the default python3 is 3.9); fall back to the default if unavailable
  dnf install -y -q python3.11 python3.11-pip >/dev/null 2>&1 || echo "note: python3.11 not available, using the default python3"
  say "Node.js 22 (the Jira MCP server runs through npx)"
  if ! command -v node >/dev/null 2>&1 || ! node -v | grep -q '^v22'; then
    dnf install -y -q nodejs22 nodejs22-npm >/dev/null 2>&1 || {
      echo "nodejs22 is not in the Amazon Linux repo; using NodeSource"
      curl -fsSL https://rpm.nodesource.com/setup_22.x | bash -
      dnf install -y -q nodejs
    }
  fi
  command -v npx >/dev/null 2>&1 || { echo "npx is missing after the Node install" >&2; exit 1; }
  say "GitHub CLI"
  if ! command -v gh >/dev/null 2>&1; then
    curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo -o /etc/yum.repos.d/gh-cli.repo
    dnf install -y -q gh
  fi
  say "Docker Compose plugin (not in the Amazon Linux repo)"
  if ! docker compose version >/dev/null 2>&1; then
    install -d -m 755 /usr/local/lib/docker/cli-plugins
    curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$ARCH" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose && chmod 755 /usr/local/lib/docker/cli-plugins/docker-compose \
      || echo "warning: could not install the compose plugin (docker build still works)" >&2
  fi
  say "sudo must find /usr/local/bin (Amazon Linux's sudo does not search it by default)"
  printf 'Defaults secure_path = /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n' >/etc/sudoers.d/90-claude-agent-path
  chmod 440 /etc/sudoers.d/90-claude-agent-path; visudo -cf /etc/sudoers.d/90-claude-agent-path >/dev/null
  export PYTHON=$(command -v python3.11 || command -v python3)
fi

say "Swap (2 GB, only if the box has none)"
if [ "$(swapon --show --noheadings | wc -l)" = 0 ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi

say "Docker and git defaults"
systemctl enable --now docker >/dev/null 2>&1
git config --system url."https://github.com/".insteadOf "git@github.com:"      # clones use HTTPS + the GitHub login, no SSH keys
[ -z "$ADMIN" ] || usermod -aG docker "$ADMIN"

say "Platform (claude-agent-box)"
"$HERE/claude-agent-box/install-box.sh"
# the person who ran this setup is the box admin: the MCP allow-list does not restrict them
[ -z "$ADMIN" ] || { printf '%s\n' "$ADMIN" >/etc/claude-code/mcp-all-users.txt; chmod 644 /etc/claude-code/mcp-all-users.txt; }

say "Repo list and context"
install -d -m 750 /etc/claude-agent
[ -f /etc/claude-agent/repos.txt ] || install -m 644 "$HERE/data/repos.txt" /etc/claude-agent/repos.txt
install -d -m 755 /srv/vylo
[ -f /srv/vylo/context.md ] || install -m 644 "$HERE/data/context.md" /srv/vylo/context.md
[ -f /etc/claude-agent/people.json ] || printf '{"people": []}\n' | install -m 640 -o root -g root /dev/stdin /etc/claude-agent/people.json

cat <<DONE

Done on ${PRETTY_NAME}. Admin user: ${ADMIN:-none detected}. Nothing is started yet. Next, in this order:
  1. sudo ./configure-box.sh        Chat app settings, the Chat service-account key, the Jira kit
  2. sudo ./github-bot-login.sh     paste the fine-grained GitHub token
  3. sudo vylo-mirror-sync.sh       mirror the repos from GitHub
  4. sudo ./verify-box.sh           check everything; add --online to test Google and GitHub too
  5. sudo systemctl enable --now claude-agent-bridge
  6. sudo add-colleague.sh <name> <work-email>
See README-EC2.md.
DONE
