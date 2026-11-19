#!/usr/bin/env bash
# PreToolUse hook, policy v2.
#
#   Reads are free. An agent may commit, push to a NON-protected branch and open a DRAFT pull request.
#   It may not write to production (kubectl/helm/terraform/aws/ssh/curl writes, write tools of the prod
#   MCP servers), merge, mark ready, approve or close a PR, or push to master/main/release*.
#
# deny = refused with a reason the agent sees; ask = a prompt to the user; no output = rules decide.
#   Base copies (only for a person whose agent serves several sessions, see agent-max-sessions): each repo
#   under the vylo folder is one checkout shared by all their sessions, so nothing may change it there. Edit,
#   Write and NotebookEdit inside it are denied, and so are git commands that move its branch or change its
#   files (commit, checkout/switch to anything but the default branch, pull without --ff-only, stash, add, ...).
#   Work happens in a worktree: vylo/.worktrees/<repo>-<branch>, or Claude's own <repo>/.claude/worktrees/*.
#   A path belongs to the repo whose .git is nearest above it, so a worktree nested in a base copy is not one.
#   `cd DIR` earlier in the same command is followed, so `cd repo && git commit` is judged in repo.
#
# Matches command TEXT, not intent: a write hidden in a script file, `python -c`, or an unusual
# client is not caught (a base copy can still be written that way). Every deny/ask is written to the audit log.
set -o pipefail
in=$(cat)
tool=$(jq -r '.tool_name // empty' <<<"$in")
session=$(jq -r '.session_id // empty' <<<"$in")
cwd=$(jq -r '.cwd // empty' <<<"$in")
AUDIT=${CLAUDE_AUDIT_BIN:-/usr/local/bin/claude-audit.sh}

# Branches nobody pushes to from here (decision: master/main/release* as asked, plus the long-lived
# deploy branches this org uses).
PROTECTED='^(master|main|release.*|stage|staging|prod|production|develop)$'
READ_FIRST='^(get|list|search|read|fetch|describe|download|query|count|find|validate|analyze|lookup|check|show|view|explain|aggregate)$'
WRITE_VERBS='^(create|update|delete|remove|add|edit|move|transition|assign|link|upload|send|reply|forward|trash|untrash|label|unlabel|share|post|index|apply|patch|set|copy|merge|publish|trigger|build|restart|scale|spawn|stop|cancel|write|insert|drop|rename|replace|archive|respond|resolve|restore|run|execute|exec|general|start|pause|resume|complete|authenticate|reconnect|import|duplicate|convert|mark|unmark|submit|kill|rollout|deploy|invoke|put|push|approve|reject|close|reopen|enable|disable|grant|revoke|rotate|reset|flush|clear|purge|truncate|alter)$'
PROD_SERVER='^(wheelseye-(kubernetes|elasticsearch|jenkins|grafana|cubeapm|prometheus|sentry|aws|graphify)|MongoDB|redshift|lakehouse)'
LOCAL_HOST='^(localhost|127\.[0-9.]+|0\.0\.0\.0|\[?::1\]?|host\.docker\.internal|[a-z0-9-]+\.localhost)(:[0-9]+)?(/.*)?$'

decide() {  # $1 = ask|deny   $2 = reason
  local d=$1 r=$2
  if [ "$d" = ask ] && [ -n "${CLAUDE_AGENT_JOB:-}" ]; then   # a scheduled job has nobody to ask: refuse, and say so
    d=deny; r="$r Needs the user's approval, and a scheduled job cannot ask: report it on a 'NEEDS YOU:' line instead."
  fi
  set -- "$d" "$r"
  "$AUDIT" --guard "$session" "$tool" "$1" 2>/dev/null || true
  jq -nc --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:$d,permissionDecisionReason:$r}}'
  exit 0
}
protected() { [[ "$1" =~ $PROTECTED ]]; }

# ---------------------------------------------------------------- base copies (several sessions per person)
VYLO=${CLAUDE_GUARD_VYLO_DIR:-$HOME/Documents/code/vylo}
MS=""
SESSIONS_HELPER=/usr/local/bin/agent-max-sessions   # by absolute path: the agent can put its own first on PATH
if [ "$(realpath "$0" 2>/dev/null)" != /usr/local/bin/claude-guard.sh ] && [ -x "$(dirname "$0")/agent-max-sessions" ]; then
  SESSIONS_HELPER=$(dirname "$0")/agent-max-sessions                 # a bundle copy under test uses its own
fi
multi_session() {  # true when this person's agent serves more than one session; asked once per call
  if [ -z "$MS" ]; then
    MS=${CLAUDE_GUARD_MAX_SESSIONS:-$("$SESSIONS_HELPER" 2>/dev/null)}
    [[ $MS =~ ^[1-5]$ ]] || MS=1
  fi
  [ "$MS" -gt 1 ]
}
base_copy_of() {  # $1 = path; prints the base-copy root that holds it, or nothing
  local p v d
  v=$(realpath -m "$VYLO" 2>/dev/null) || return 0
  p=$(realpath -m "$1" 2>/dev/null) || return 0
  [[ $p == "$v"/* ]] || return 0
  d=$p
  while [ "$d" != "$v" ] && [ "$d" != / ]; do
    if [ -e "$d/.git" ]; then
      [ "$(dirname "$d")" = "$v" ] && [ -d "$d/.git" ] && printf '%s\n' "$d"
      return 0
    fi
    d=$(dirname "$d")
  done
}
default_branch() {  # $1 = repo
  local b
  b=$(git -C "$1" symbolic-ref --short -q refs/remotes/origin/HEAD 2>/dev/null) && { echo "${b#origin/}"; return; }
  if git -C "$1" show-ref -q --verify refs/heads/master 2>/dev/null; then echo master; else echo main; fi
}
deny_base() {  # $1 = base-copy root
  local r; r=$(basename "$1")
  decide deny "This is the shared copy of $r, used by all your sessions. Make your own worktree first: git -C $1 worktree add ../.worktrees/$r-<branch> -b <branch>"
}
git_reads() {  # $1 = subcommand   $2 = its arguments: true when it only reads the repo (or adds a worktree / fetches)
  local x; local -a a pos=()
  read -r -a a <<<"$2"
  for x in "${a[@]}"; do [[ $x == -* ]] || pos+=("$x"); done
  case "$1" in
    ""|status|log|diff|show|rev-parse|rev-list|ls-files|ls-tree|ls-remote|cat-file|blame|annotate|grep|describe|shortlog| \
    merge-base|name-rev|for-each-ref|show-ref|show-branch|count-objects|help|version|var|check-ignore|check-attr| \
    whatchanged|diff-tree|diff-files|diff-index|cherry|range-diff|fetch|worktree) return 0 ;;
    branch) [ "${#pos[@]}" -eq 0 ] && ! [[ " $2 " =~ [[:space:]](-f|--force|-D|-d|--delete|-m|-M|--move|-c|-C|--copy|-u|--unset-upstream|--edit-description|--set-upstream-to(=[^[:space:]]*)?)[[:space:]] ]] ;;
    tag)    [ "${#pos[@]}" -eq 0 ] || [[ " $2 " =~ [[:space:]](-l|--list)[[:space:]] ]] ;;
    remote) [ "${#pos[@]}" -eq 0 ] || [ "${pos[0]}" = show ] || [ "${pos[0]}" = get-url ] ;;
    config) [[ " $2 " =~ [[:space:]](--get|--get-all|--get-regexp|--get-urlmatch|-l|--list)[[:space:]] ]] ;;
    stash)  [ "${pos[0]:-}" = list ] || [ "${pos[0]:-}" = show ] ;;
    reflog) [ "${pos[0]:-}" != expire ] && [ "${pos[0]:-}" != delete ] ;;
    *) return 1 ;;
  esac
}
check_base_git() {  # $1 = repo dir the command runs in ("?" = unknown)   $2 = subcommand   $3 = its arguments
  local base sub=$2 x def cur
  local -a a pos=()
  if [ "$1" = "?" ]; then                             # a variable, GIT_DIR or --git-dir: the repo cannot be read off the text
    git_reads "$sub" "$3" && return 0
    multi_session || return 0
    decide deny "Cannot tell which repo this git command changes (it is named by a variable, GIT_DIR or --git-dir). Your sessions share each repo's copy, so write the path out: cd ~/Documents/code/vylo/.worktrees/<repo>-<branch> && git $sub ..."
  fi
  base=$(base_copy_of "$1"); [ -n "$base" ] || return 0
  git_reads "$sub" "$3" && return 0
  multi_session || return 0
  read -r -a a <<<"$3"
  case "$sub" in
    checkout|switch)
      for x in "${a[@]}"; do
        case "$x" in
          -b|-B|-c|-C|--orphan|--detach|-d|--force-create|--create|--) deny_base "$base" ;;
          -*) ;;
          *) pos+=("$x") ;;
        esac
      done
      def=$(default_branch "$base")
      [ "${#pos[@]}" -eq 1 ] && [ "${pos[0]}" = "$def" ] || deny_base "$base" ;;
    pull)
      def=$(default_branch "$base")
      cur=$(git -C "$base" symbolic-ref --short -q HEAD 2>/dev/null || true)
      [[ " $3 " == *" --ff-only "* ]] && [ "$cur" = "$def" ] || deny_base "$base" ;;
    *) deny_base "$base" ;;                           # anything that is not a read: commit, stash, update-ref, bisect...
  esac
  return 0
}
resolve_dir() {  # $1 = directory as typed, $2 = directory it is relative to. Prints "?" when it cannot be known.
  local d=$1 u rest h
  d=${d#[\"\']}; d=${d%[\"\']}
  case "$d" in
    '$HOME'|'${HOME}') d=$HOME ;;
    '$HOME/'*) d=$HOME/${d#\$HOME/} ;;
    '${HOME}/'*) d=$HOME/${d#\$\{HOME\}/} ;;
  esac
  case "$d" in
    ""|"~") d=$HOME ;;
    "~/"*) d=$HOME/${d#\~/} ;;
    "~"*) u=${d#\~}; rest=""; [[ $u == */* ]] && { rest=/${u#*/}; u=${u%%/*}; }
          h=$(getent passwd "$u" 2>/dev/null | cut -d: -f6); [ -n "$h" ] || { echo "?"; return; }; d=$h$rest ;;
    /*) ;;
    *) [ "$2" = "?" ] && { echo "?"; return; }; d=$2/$d ;;
  esac
  [[ $d == *'$'* || $d == *'`'* ]] && { echo "?"; return; }
  realpath -m "$d" 2>/dev/null || printf '%s\n' "$d"
}

# ---------------------------------------------------------------- git
git_push() {  # $1 = -C dir (may be empty)   $2 = arguments after "push"
  local dir=$1 force="" x r dst cur where
  local -a a pos=()
  read -r -a a <<<"$2"
  for x in "${a[@]}"; do
    case "$x" in
      --all|--mirror|--tags|--prune|--follow-tags) decide deny "git push $x can touch protected branches or tags: push one named branch." ;;
      --delete|-d) decide deny "Deleting remote refs is not allowed." ;;
      --force|-f) force=1 ;;
      -*) ;;
      *) pos+=("$x") ;;
    esac
  done
  where=${dir:-$wd}
  cur=$(git -C "${where:-.}" symbolic-ref --short -q HEAD 2>/dev/null || true)
  if [ "${#pos[@]}" -le 1 ]; then
    [ -n "$cur" ] || decide deny "Cannot tell which branch this push targets: name it (git push origin <branch>)."
    protected "$cur" && decide deny "Pushing '$cur' is not allowed. Work on a feature branch and open a draft PR."
  else
    for r in "${pos[@]:1}"; do
      [[ $r == :* ]] && decide deny "Deleting remote refs is not allowed."
      r=${r#+}; dst=$r
      [[ $r == *:* ]] && dst=${r#*:}
      dst=${dst#refs/heads/}
      [[ $dst == refs/* ]] && decide deny "Only branch pushes are allowed (not '$dst')."
      if [ "$dst" = HEAD ]; then
        [ -n "$cur" ] || decide deny "Cannot tell which branch HEAD is here: name it (git push origin <branch>)."
        dst=$cur
      fi
      protected "$dst" && decide deny "Pushing to '$dst' is not allowed. Push a feature branch and open a draft PR."
    done
  fi
  [ -n "$force" ] && decide ask "Force push: plain --force can overwrite others' commits (--force-with-lease is allowed)."
  return 0
}

check_git() {  # $1 = whole segment starting with "git"
  local -a t; local i=1 dir="" sub rest
  read -r -a t <<<"$1"
  [ -n "$GITENV" ] && dir="?"                         # GIT_DIR=/GIT_WORK_TREE= before the command
  while [ "$i" -lt "${#t[@]}" ]; do
    case "${t[$i]}" in
      -C) [ "$dir" = "?" ] || dir=$(resolve_dir "${t[$((i+1))]:-}" "${dir:-$wd}"); i=$((i+2)) ;;
      --git-dir|--work-tree) dir="?"; i=$((i+2)) ;;
      --git-dir=*|--work-tree=*) dir="?"; i=$((i+1)) ;;
      -c|--exec-path|--namespace|--super-prefix) i=$((i+2)) ;;
      -*) i=$((i+1)) ;;
      *) break ;;
    esac
  done
  sub=${t[$i]:-}; rest=${t[*]:$((i+1))}
  check_base_git "${dir:-$wd}" "$sub" "$rest"
  case "$sub" in
    push)   git_push "$dir" "$rest" ;;
    reset)  [[ " $rest " == *" --hard "* ]] && decide ask "git reset --hard discards work." ;;
    clean)  [[ "$rest" =~ (^|[[:space:]])-[a-zA-Z]*f ]] && decide ask "git clean -f deletes untracked files." ;;
    branch) [[ "$rest" =~ (^|[[:space:]])(-D|--delete[[:space:]]+--force|-d[[:space:]]+-f)([[:space:]]|$) ]] && decide ask "Force-deleting a branch." ;;
  esac
}

# ---------------------------------------------------------------- gh
check_gh() {  # $1 = whole segment starting with "gh"
  local -a t; read -r -a t <<<"$1"
  local g=${t[1]:-} v=${t[2]:-} s=" $1 "
  case "$g" in
    pr)
      case "$v" in
        view|list|diff|checks|status|comment|edit|checkout|reopen|update-branch) ;;
        create)
          if [[ $s == *" --draft "* || $s == *" -d "* ]]; then :; else
            decide deny "Always open pull requests as drafts: re-run gh pr create with --draft."
          fi ;;
        review)
          if [[ $s == *" --approve "* || $s == *" -a "* ]]; then decide deny "Approving a PR is a human decision."; fi ;;
        ready) decide deny "Marking a PR ready for review is the user's decision." ;;
        merge) decide deny "Merging is not allowed from the agent." ;;
        close) decide deny "Closing a PR is not allowed from the agent." ;;
        *) decide ask "gh pr $v" ;;
      esac ;;
    issue)
      case "$v" in view|list|status|comment|develop) ;; *) decide ask "gh issue $v" ;; esac ;;
    run|workflow)
      case "$v" in view|list|watch|download) ;; *) decide deny "gh $g $v can trigger or change a pipeline." ;; esac ;;
    release) case "$v" in view|list|download) ;; *) decide deny "Releases are not created from the agent." ;; esac ;;
    repo)    case "$v" in view|list|clone) ;; *) decide deny "gh repo $v changes repository settings." ;; esac ;;
    auth)    case "$v" in status) ;; *) decide deny "gh auth $v is not allowed (it can print or change credentials)." ;; esac ;;
    api)
      if [[ $s == *" graphql "* ]]; then
        [[ "$s" =~ [Mm]utation ]] && decide deny "GraphQL mutations are writes."
      elif [[ "$s" =~ (-X|--method)[[:space:]=]*(POST|PUT|PATCH|DELETE) ]] \
        || { [[ "$s" =~ (^|[[:space:]])(-f|-F|--field|--raw-field|--input)([[:space:]=]|$) ]] && ! [[ "$s" =~ (-X|--method)[[:space:]=]*GET ]]; }; then
        decide deny "gh api writes are not allowed."
      fi ;;
    search|browse|status|help|version|--version|completion|"") ;;
    secret|variable|ssh-key|gpg-key|codespace|cache|ruleset|org|project|label|gist|extension|alias|config)
      case "$v" in list|view|get) ;; *) decide deny "gh $g $v changes remote state." ;; esac ;;
    *) decide ask "gh $g $v" ;;
  esac
}

# ---------------------------------------------------------------- aws / http / misc
check_aws() {
  local -a t; read -r -a t <<<"$1"
  local i=1 svc="" verb="" x
  while [ "$i" -lt "${#t[@]}" ]; do
    x=${t[$i]}
    case "$x" in
      --profile|--region|--output|--query|--endpoint-url|--cli-read-timeout|--cli-connect-timeout|--ca-bundle|--color) i=$((i+2)) ;;
      --*) i=$((i+1)) ;;
      *) if [ -z "$svc" ]; then svc=$x; elif [ -z "$verb" ]; then verb=$x; break; fi; i=$((i+1)) ;;
    esac
  done
  case "$svc" in
    sts|"") return 0 ;;
    configure) case "$verb" in list|get|list-profiles) return 0 ;; *) decide deny "aws configure $verb changes local AWS credentials." ;; esac ;;
    s3)
      case "$verb" in
        ls|presign) return 0 ;;
        cp|sync)
          local last=${t[$((${#t[@]}-1))]}
          [[ $last == s3://* ]] && decide deny "Writing to S3 is not allowed (production data)."
          return 0 ;;
        *) decide deny "aws s3 $verb writes to S3." ;;
      esac ;;
  esac
  if [[ "$verb" =~ ^(describe|get|list|head|ls|query|scan|batch-get|lookup|search|filter|tail|view|show|help|wait|select|simulate|validate|estimate|generate) ]]; then
    return 0
  fi
  decide deny "aws $svc $verb is not a read call; production is read-only."
}

local_only() {  # $1 = command segment: true if every URL/host in it is local
  local hosts h found=0 tok
  hosts=$(grep -oE '[a-z][a-z0-9+.-]*://[^/[:space:]"'"'"']+' <<<"$1" | sed -E 's#^[a-z][a-z0-9+.-]*://##; s#^[^@]*@##')
  for h in $hosts; do found=1; [[ $h =~ $LOCAL_HOST ]] || return 1; done
  [ "$found" = 1 ] && return 0
  for tok in $1; do [[ $tok =~ $LOCAL_HOST ]] && return 0; done
  return 1
}

check_http() {
  local s=" $1 "
  if [[ "$s" =~ (-X|--request)[[:space:]=]*(POST|PUT|PATCH|DELETE) ]] \
     || [[ "$s" =~ (^|[[:space:]])(-d|--data|--data-raw|--data-binary|--data-urlencode|-F|--form|-T|--upload-file|--json|--post-data|--post-file)([[:space:]=]|$) ]]; then
    local_only "$1" || decide deny "HTTP writes to non-local hosts are not allowed (production is read-only)."
  fi
}

check_db_cli() {  # best effort: a write statement in a DB shell invocation on a non-local host
  local l; l=$(tr 'A-Z' 'a-z' <<<"$1")
  if [[ "$l" =~ (insertone|insertmany|updateone|updatemany|deleteone|deletemany|replaceone|bulkwrite|findandmodify|\.drop\(|\.remove\(|dropdatabase|createindex|[^a-z](insert|update|delete|drop|alter|truncate|create|grant|flushall|flushdb)[^a-z_]) ]]; then
    local_only "$1" && return 0
    decide deny "Write statements against a database are not allowed from the agent."
  fi
}

scan() {  # $1 = command text; $2 = recursion depth
  local text=$1 depth=${2:-0} seg inner
  [ "$depth" -gt 3 ] && return 0
  # Drop heredoc bodies and -m "message" arguments so prose cannot look like a command.
  text=$(awk 'BEGIN{d=""} d!=""{ if($0==d) d=""; next } { print; if (match($0, /<<-?[ ]*["'"'"']?[A-Za-z_][A-Za-z0-9_]*/)) { s=substr($0,RSTART,RLENGTH); gsub(/<<-?[ ]*["'"'"']?/,"",s); d=s } }' <<<"$text" \
         | sed -E "s/(^|[[:space:]])(-m|--message)[= ]*(\"[^\"]*\"|'[^']*')//g")
  if [[ "$text" =~ curl[^|]*\|[[:space:]]*(sudo[[:space:]]+)?(ba|z)?sh ]]; then
    decide ask "Piping a download into a shell."
  fi
  text=$(printf '%s' "$text" | sed -E 's/\$\(/\n/g; s/`/\n/g; s/[()]/\n/g; s/(&&|\|\||;|\|)/\n/g')
  while IFS= read -r seg; do
    seg=$(sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' <<<"$seg")
    GITENV=""
    [[ $seg =~ (^|[[:space:]])(GIT_DIR|GIT_WORK_TREE)= ]] && GITENV=1
    [[ $seg =~ ^export[[:space:]].*(GIT_DIR|GIT_WORK_TREE)= ]] && wd="?"     # for the rest of the command
    while [[ $seg =~ ^([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*|sudo|env|time|nohup|nice|command|exec|xargs|do|then|else|elif|if|while|until|!|\{)[[:space:]]+(.*)$ ]]; do seg=${BASH_REMATCH[2]}; done
    [ -z "$seg" ] && continue
    case "$seg" in
      cd|"cd "*|"pushd "*)
        local arg=${seg#cd}; arg=${arg#pushd}; arg=$(sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' <<<"$arg")
        [ "$arg" = - ] || wd=$(resolve_dir "$arg" "$wd") ;;
      "bash -c "*|"sh -c "*|"zsh -c "*|"bash -lc "*|"sh -lc "*)
        inner=${seg#* -*c }; inner=$(sed -E "s/^[\"']//; s/[\"']\$//" <<<"$inner"); scan "$inner" $((depth+1)) ;;
      git|git\ *) check_git "$seg" ;;
      gh|gh\ *) check_gh "$seg" ;;
      curl*|wget*|http\ *|https\ *) check_http "$seg" ;;
      aws|aws\ *) check_aws "$seg" ;;
      kubectl*|oc\ *|helm*)
        if [[ "$seg" =~ rollout[[:space:]]+(status|history) ]]; then :
        elif [[ "$seg" =~ (^|[[:space:]])(apply|delete|patch|scale|rollout|exec|edit|create|replace|set|drain|cordon|uncordon|taint|annotate|label|cp|run|expose|install|upgrade|uninstall|rollback|attach|debug|autoscale)([[:space:]]|$) ]]; then
          decide deny "Cluster writes are not allowed (production is read-only)."
        fi ;;
      terraform*|tofu*)
        [[ "$seg" =~ (^|[[:space:]])(apply|destroy|import|taint|untaint|force-unlock|refresh)([[:space:]]|$) || "$seg" =~ state[[:space:]]+(rm|mv|push|replace-provider) || "$seg" =~ workspace[[:space:]]+(new|delete) ]] \
          && decide deny "Infrastructure changes are not allowed." ;;
      pulumi*|cdk*|serverless*|sam\ *|eksctl*)
        [[ "$seg" =~ (^|[[:space:]])(up|destroy|refresh|import|cancel|deploy|create|delete|upgrade|scale|set)([[:space:]]|$) ]] \
          && decide deny "Infrastructure changes are not allowed." ;;
      ansible*) [[ "$seg" == *"--check"* || "$seg" == *" -C"* ]] || decide deny "Ansible runs change remote hosts." ;;
      gcloud*|az\ *|gsutil*)
        [[ "$seg" =~ (^|[[:space:]])(list|ls|describe|show|get|get-[a-z-]+|read|cat|info|version|help|config|auth)([[:space:]]|$) ]] \
          || decide deny "Cloud CLI call that may write; production is read-only." ;;
      ssh*|scp*|sftp*) decide deny "Remote shells and copies are not allowed (production is read-only)." ;;
      rsync*) [[ "$seg" =~ (^|[[:space:]])[^[:space:]/]*[@][^[:space:]]+|[^[:space:]/]+:[^[:space:]]*|rsync:// ]] \
                && decide deny "rsync to or from a remote host is not allowed." ;;
      mongosh*|mongo\ *|psql*|mysql*|redis-cli*|clickhouse-client*|cqlsh*) check_db_cli "$seg" ;;
      docker\ push*|docker\ login*|npm\ publish*|yarn\ publish*|pnpm\ publish*|twine\ upload*|cargo\ publish*|gem\ push*|flutter\ pub\ publish*|mvn*deploy*|gradle*publish*|./gradlew*publish*)
        decide deny "Publishing artifacts is not allowed from the agent." ;;
    esac
  done <<<"$text"
}

# ---------------------------------------------------------------- MCP
mcp_check() {
  local rest=${tool#mcp__} server name lname first second
  server=${rest%%__*}; name=${rest#*__}

  # Per-user MCP allow-list. ${MCP_POLICY_DIR:-/etc/claude-code}/mcp-allow.txt lists substrings of the MCP servers agents may
  # use (jira, github); the users in ${MCP_POLICY_DIR:-/etc/claude-code}/mcp-all-users.txt (admins) are unrestricted. This also
  # covers the claude.ai connectors (Gmail, Drive, Calendar, Notion ...) that arrive with a colleague's OWN
  # Claude login: they are never needed here and would reach that person's mail and files.
  if ! grep -qxF "$(id -un)" ${MCP_POLICY_DIR:-/etc/claude-code}/mcp-all-users.txt 2>/dev/null; then
    local a ok=0
    while IFS= read -r a; do [ -n "$a" ] && [[ $server == *"$a"* ]] && ok=1; done < ${MCP_POLICY_DIR:-/etc/claude-code}/mcp-allow.txt 2>/dev/null
    [ "$ok" = 1 ] || decide deny "This MCP server is not enabled for agents (${server}); only Jira and GitHub are."
  fi
  name=$(sed -E 's/^[a-z0-9]+_(mcp|oss)-//; s/^(notion|jira|confluence)[-_]//' <<<"$name")
  lname=$(sed -E 's/([a-z0-9])([A-Z])/\1_\2/g' <<<"$name" | tr 'A-Z-' 'a-z_')
  local -a toks; IFS=_ read -r -a toks <<<"$lname"
  first=${toks[0]:-}; second=${toks[1]:-}
  local payload; payload=$(jq -c '.tool_input // {}' <<<"$in" | tr 'A-Z' 'a-z')
  local is_prod=0 is_atl=0 is_gh=0 write=0 t
  [[ $server =~ $PROD_SERVER ]] && is_prod=1
  [[ $server =~ (jira|atlassian|confluence) || $lname =~ (jira|confluence) ]] && is_atl=1
  [[ $server =~ github ]] && is_gh=1

  # SQL-ish query tools: judge the statement, not the tool name.
  local wv=0
  for t in "${toks[@]}"; do [[ $t =~ $WRITE_VERBS && $t != run && $t != execute ]] && wv=1; done
  if [[ $lname =~ (^|_)query(_|$) ]] && [ "$is_prod" = 1 ] && [ "$wv" = 0 ]; then
    if [[ $payload =~ (^|[^a-z_])(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|unload|merge|call|exec|execute|vacuum|replace)([^a-z_]|$) ]]; then
      decide deny "Write statements are not allowed on production data stores."
    fi
    return 0
  fi
  if [ "$lname" = aggregate ] && [[ $payload == *'"$out"'* || $payload == *'"$merge"'* ]]; then
    decide deny "Aggregations that write ($out/$merge) are not allowed on production data."
  fi

  # Read tools: a read verb first (get_create_fields), or well-known read-only names.
  [[ $first =~ $READ_FIRST ]] && return 0
  [[ $lname =~ ^(label_(names|values)|metric_metadata|range_query|exemplar_query|series|targets_metadata|tsdb_stats|runtime_info|build_info|wal_replay_status|docs_.*|config|flags|healthy|ready|alertmanagers|targets|rules|alerts)$ ]] && return 0

  for t in "${toks[@]}"; do [[ $t =~ $WRITE_VERBS ]] && write=1; done
  [[ $lname == top_up* ]] && write=1
  [ "$write" = 0 ] && return 0

  if [ "$is_prod" = 1 ]; then
    decide deny "Production systems are read-only for agents (${server})."
  fi
  if [ "$is_atl" = 1 ]; then
    [[ $lname =~ (^|_)delete(_|$) ]] && decide deny "Deleting Jira/Confluence content is not allowed."
    [[ $lname =~ (^|_)comment(_|$) ]] && return 0       # comments are allowed
    decide ask "Jira/Confluence change: ${lname}."
  fi
  if [ "$is_gh" = 1 ]; then
    [[ $lname =~ (^|_)merge(_|$) ]] && decide deny "Merging is not allowed from the agent."
    if [[ $lname == create_pull_request ]]; then
      [ "$(jq -r '.tool_input.draft // false' <<<"$in")" = true ] || decide deny "Always open pull requests as drafts (set draft: true)."
      return 0
    fi
    [[ $lname =~ review ]] && [[ $payload == *approve* ]] && decide deny "Approving a PR is a human decision."
    [[ $lname =~ (^|_)comment(_|$) ]] && return 0
  fi
  decide ask "Tool may write or act on a remote system: ${server}/${name}."
}

wd=${cwd:-$PWD}   # the directory a git command runs in; `cd` segments in scan move it ("?" = unknown)
GITENV=""
case "$tool" in
  Bash) scan "$(jq -r '.tool_input.command // empty' <<<"$in")" ;;
  Edit|Write|NotebookEdit|MultiEdit)
    f=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' <<<"$in")
    if [ -n "$f" ]; then
      b=$(base_copy_of "$f")
      [ -n "$b" ] && multi_session && deny_base "$b"
    fi ;;
  mcp__*) mcp_check ;;
esac
exit 0
