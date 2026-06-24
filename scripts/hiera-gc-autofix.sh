#!/usr/bin/env bash
#
# hiera-gc-autofix.sh
#
# Run `hiera-gc --fix` over a set of Puppet environment git checkouts and
# propose the cleanup to Gerrit for human review, idempotently. Designed to be
# run periodically (cron) and unattended.
#
# Key behaviours:
#   * Each environment is processed independently. One bad environment is
#     logged and skipped; it never aborts the run.
#   * Idempotency comes from the live Gerrit server, not from local state. If
#     an automated review for an environment is still open (not merged), the
#     run reuses its Change-Id so Gerrit records a new patchset instead of
#     opening a second review.
#   * The script coexists with other tools that propose changes on the same
#     repos (those tools never run concurrently). It isolates its own reviews
#     by a fixed Gerrit topic plus a marker trailer, always bases its commit on
#     the clean branch tip, and stages only the files hiera-gc changed.
#   * It refuses to fix a tree whose modules are not deployed, because a blind
#     analyser would classify live data as unused and delete it.
#
# Pushes for review only. It never auto-approves or submits, so the human and
# catalog-diff review remains the gate before anything merges.
#
# See scripts/hiera-gc-autofix.conf.example for the config format and the
# "Periodic automated cleanup" section of README.md for the full description.

set -euo pipefail
IFS=$'\n\t'

# --------------------------------------------------------------------------
# Defaults (all overridable by flags or the environment)
# --------------------------------------------------------------------------
CONFIG="${HIERA_GC_AUTOFIX_CONFIG:-/etc/hiera-gc-autofix/envs.conf}"
# Global Puppet code dir, used as --code-dir when a checkout is itself an
# environment (so global modules and shared hieradata are visible).
CODE_DIR_DEFAULT="${HIERA_GC_AUTOFIX_CODE_DIR:-/etc/puppetlabs/code}"
TOPIC="${HIERA_GC_AUTOFIX_TOPIC:-hiera-gc}"
FIX_KINDS="${HIERA_GC_AUTOFIX_FIX_KINDS:-unused,redundant,orphans,stale_files}"
ALLOWLIST="${HIERA_GC_AUTOFIX_ALLOWLIST:-}"
MAX_REMOVALS="${HIERA_GC_AUTOFIX_MAX_REMOVALS:-0}"   # 0 = no cap
HIERA_GC="${HIERA_GC:-hiera-gc}"
# Gerrit ssh username for `gerrit query` and the hook scp. Empty means derive
# it from the gerrit remote URL, then gitreview.username.
GERRIT_USER="${HIERA_GC_AUTOFIX_GERRIT_USER:-}"
# Install each environment's Puppetfile modules before analysis so hiera-gc
# sees the real consumers. 1 = on, 0 = off. The command is run with the
# environment as its working directory.
PUPPETFILE_INSTALL="${HIERA_GC_AUTOFIX_PUPPETFILE_INSTALL:-1}"
PUPPETFILE_CMD="${HIERA_GC_AUTOFIX_PUPPETFILE_CMD:-r10k puppetfile install}"
# Optional r10k config file (r10k.yaml). Set it in the config file
# (r10k_config = /path/to/r10k.yaml), with --r10k-config, or this env var.
# When set it is passed to r10k as a global option: r10k --config <file> ...
R10K_CONFIG="${HIERA_GC_AUTOFIX_R10K_CONFIG:-}"
R10K_CONFIG_CLI=""
LOCK_FILE="${HIERA_GC_AUTOFIX_LOCK:-/var/lock/hiera-gc-autofix.lock}"
LOG_DIR="${HIERA_GC_AUTOFIX_LOG_DIR:-}"
BOT_NAME="${BOT_NAME:-hiera-gc autofix}"
BOT_EMAIL="${BOT_EMAIL:-hiera-gc-autofix@nectar.org.au}"
SSH_CONNECT_TIMEOUT="${HIERA_GC_AUTOFIX_SSH_TIMEOUT:-10}"
DRY_RUN=0
NO_PUSH=0

# Marker trailer token; identifies our own commits even if the topic or bot
# account is ever shared with another tool.
MARKER_TOKEN="Hiera-GC-Autofix"

# --------------------------------------------------------------------------
# Run state
# --------------------------------------------------------------------------
declare -a POSITIONAL=()
declare -a ENTRY_REPO=()
declare -a ENTRY_ENV=()
declare -a ENTRY_CODE=()
declare -a RESULTS=()
declare -a HG_CMD=()
declare -a PF_CMD=()
OVERALL_RC=0
CUR_ENV="main"
ENTRY_STATE=""
LOG_FILE=""
TMPDIR_RUN=""

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
log() {
  local line
  line="$(date -u +%FT%TZ) [${CUR_ENV}] $*"
  if [ -n "$LOG_FILE" ]; then
    printf '%s\n' "$line" | tee -a "$LOG_FILE" >&2
  else
    printf '%s\n' "$line" >&2
  fi
}
warn() { log "WARNING: $*"; }
err() { log "ERROR: $*"; }
die() { err "$*"; exit 2; }

usage() {
  cat >&2 <<'EOF'
Usage: hiera-gc-autofix.sh [options] [repo:env[:code_dir] ...]

Runs hiera-gc --fix over the configured Puppet environment checkouts and
proposes the cleanup to Gerrit, updating an existing open review in place.

Two checkout layouts are detected automatically:
  * the checkout is itself an environment (a hiera.yaml at its root): the
    environment name is the directory name and its parent is used as the
    environments root (hiera-gc --env-dir), with --code-dir defaulting to
    /etc/puppetlabs/code for global modules and shared data;
  * the checkout is a full code tree: the environment lives at
    <code_dir>/environments/<env> and the env name must be given.

Options:
  --config FILE       Config file of environments (default /etc/hiera-gc-autofix/envs.conf)
  --code-dir-default DIR  Global code dir for the self-environment layout (default /etc/puppetlabs/code)
  --topic NAME        Gerrit topic used to find and group reviews (default hiera-gc)
  --fix-kinds LIST    hiera-gc --fix-kinds (default unused,redundant,orphans,stale_files)
  --allowlist FILE    hiera-gc --allowlist (recommended when 'unused' is in scope)
  --max-removals N    Skip an environment if a run would change more than N items (0 = no cap)
  --hiera-gc CMD      How to invoke hiera-gc (default 'hiera-gc'; e.g. 'python3 -m hiera_gc')
  --gerrit-user NAME  Gerrit ssh username (default: from the gerrit remote URL / gitreview.username)
  --puppetfile-cmd CMD  Command to install an env's Puppetfile modules (default 'r10k puppetfile install')
  --no-puppetfile-install  Do not install Puppetfile modules; analyse the modules already present
  --r10k-config FILE  r10k config file (r10k.yaml), passed as 'r10k --config FILE ...'
  --lock FILE         Lock file for the self-overlap guard (default /var/lock/hiera-gc-autofix.lock)
  --log-dir DIR       Also append logs to DIR/hiera-gc-autofix-YYYYMMDD.log
  --dry-run           Refresh and analyse, report what would change, make no edits or pushes
  --no-push           Apply and commit locally, but do not run git review
  -h, --help          Show this help

Positional entries (override the config file) are 'repo', 'repo:env' or
'repo:env:code_dir'. For a self-environment checkout, 'repo' alone is enough.

Environment: BOT_NAME and BOT_EMAIL set the sign-off identity.
EOF
}

# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --config) CONFIG="${2:-}"; shift 2 ;;
      --config=*) CONFIG="${1#*=}"; shift ;;
      --code-dir-default) CODE_DIR_DEFAULT="${2:-}"; shift 2 ;;
      --code-dir-default=*) CODE_DIR_DEFAULT="${1#*=}"; shift ;;
      --topic) TOPIC="${2:-}"; shift 2 ;;
      --topic=*) TOPIC="${1#*=}"; shift ;;
      --fix-kinds) FIX_KINDS="${2:-}"; shift 2 ;;
      --fix-kinds=*) FIX_KINDS="${1#*=}"; shift ;;
      --allowlist) ALLOWLIST="${2:-}"; shift 2 ;;
      --allowlist=*) ALLOWLIST="${1#*=}"; shift ;;
      --max-removals) MAX_REMOVALS="${2:-}"; shift 2 ;;
      --max-removals=*) MAX_REMOVALS="${1#*=}"; shift ;;
      --hiera-gc) HIERA_GC="${2:-}"; shift 2 ;;
      --hiera-gc=*) HIERA_GC="${1#*=}"; shift ;;
      --gerrit-user) GERRIT_USER="${2:-}"; shift 2 ;;
      --gerrit-user=*) GERRIT_USER="${1#*=}"; shift ;;
      --puppetfile-cmd) PUPPETFILE_CMD="${2:-}"; shift 2 ;;
      --puppetfile-cmd=*) PUPPETFILE_CMD="${1#*=}"; shift ;;
      --no-puppetfile-install) PUPPETFILE_INSTALL=0; shift ;;
      --r10k-config) R10K_CONFIG_CLI="${2:-}"; shift 2 ;;
      --r10k-config=*) R10K_CONFIG_CLI="${1#*=}"; shift ;;
      --lock) LOCK_FILE="${2:-}"; shift 2 ;;
      --lock=*) LOCK_FILE="${1#*=}"; shift ;;
      --log-dir) LOG_DIR="${2:-}"; shift 2 ;;
      --log-dir=*) LOG_DIR="${1#*=}"; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --no-push) NO_PUSH=1; shift ;;
      -h|--help) usage; exit 0 ;;
      --) shift; while [ $# -gt 0 ]; do POSITIONAL+=("$1"); shift; done ;;
      -*) die "unknown option: $1" ;;
      *) POSITIONAL+=("$1"); shift ;;
    esac
  done
  case "$MAX_REMOVALS" in
    ''|*[!0-9]*) die "--max-removals must be a non-negative integer" ;;
  esac
}

# --------------------------------------------------------------------------
# Setup: lock, log dir, hiera-gc command split, temp dir
# --------------------------------------------------------------------------
setup() {
  if [ -n "$LOG_DIR" ]; then
    mkdir -p "$LOG_DIR" || die "cannot create log dir $LOG_DIR"
    LOG_FILE="$LOG_DIR/hiera-gc-autofix-$(date -u +%Y%m%d).log"
  fi
  # Split the hiera-gc command into words so 'python3 -m hiera_gc' works.
  IFS=' ' read -r -a HG_CMD <<<"$HIERA_GC"
  [ "${#HG_CMD[@]}" -gt 0 ] || die "empty --hiera-gc command"
  IFS=' ' read -r -a PF_CMD <<<"$PUPPETFILE_CMD"
  [ "${#PF_CMD[@]}" -gt 0 ] || die "empty --puppetfile-cmd command"
  TMPDIR_RUN="$(mktemp -d "${TMPDIR:-/tmp}/hiera-gc-autofix.XXXXXX")" || die "mktemp failed"
  # shellcheck disable=SC2064
  trap "rm -rf '$TMPDIR_RUN'" EXIT
}

acquire_lock() {
  exec 9>"$LOCK_FILE" || die "cannot open lock file $LOCK_FILE"
  if ! flock -n 9; then
    log "another run holds $LOCK_FILE; exiting"
    exit 0
  fi
}

# --------------------------------------------------------------------------
# Build the environment list
# --------------------------------------------------------------------------
add_entry() {
  local repo="$1" env="$2" code_dir="$3"
  if [ -z "$repo" ]; then
    warn "ignoring entry with empty repo dir"
    return 0
  fi
  # env and code_dir may be empty; process_env resolves them from the layout.
  ENTRY_REPO+=("$repo")
  ENTRY_ENV+=("$env")
  ENTRY_CODE+=("$code_dir")
}

load_entries() {
  if [ "${#POSITIONAL[@]}" -gt 0 ]; then
    local p repo env code_dir
    for p in "${POSITIONAL[@]}"; do
      IFS=':' read -r repo env code_dir _ <<<"$p"
      add_entry "$repo" "$env" "$code_dir"
    done
    return 0
  fi
  if [ ! -f "$CONFIG" ]; then
    warn "config file not found: $CONFIG"
    return 0
  fi
  local line repo env code_dir key val
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    # A "key = value" / "key=value" line (key is a bare word) is a global
    # setting, not an environment entry. Environment entries are paths.
    if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_-]*)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      val="${val%"${val##*[![:space:]]}"}"   # trim trailing whitespace
      case "$key" in
        r10k_config|r10k-config) R10K_CONFIG="$val" ;;
        *) warn "config: unknown setting '$key', ignoring" ;;
      esac
      continue
    fi
    repo=""; env=""; code_dir=""
    IFS=$' \t' read -r repo env code_dir _ <<<"$line" || true
    [ -n "$repo" ] || continue
    add_entry "$repo" "$env" "$code_dir"
  done < "$CONFIG"
}

# --------------------------------------------------------------------------
# Helpers used inside process_env
# --------------------------------------------------------------------------

# Print the name of the git remote whose URL contains the Gerrit host.
resolve_gerrit_remote() {
  local root="$1" host="$2" r url
  while IFS= read -r r; do
    [ -n "$r" ] || continue
    url="$(git -C "$root" remote get-url "$r" 2>/dev/null || true)"
    case "$url" in
      *"$host"*) printf '%s\n' "$r"; return 0 ;;
    esac
  done < <(git -C "$root" remote)
  return 1
}

# Resolve the ssh "[user@]host" and port for gerrit admin commands (gerrit
# query, scp of the hook) from the gerrit remote URL, since our raw ssh call
# does not inherit git's URL/ssh-config username handling. Falls back to
# gitreview.username and the .gitreview host/port. An explicit GERRIT_USER
# overrides the derived user. Prints "<[user@]host> <port>".
gerrit_ssh_coords() {
  local root="$1" remote="$2" gh="$3" gp="$4" url user host port rest
  url="$(git -C "$root" remote get-url "$remote" 2>/dev/null || true)"
  user=""; host=""; port=""
  case "$url" in
    ssh://*)
      rest="${url#ssh://}"; rest="${rest%%/*}"   # [user@]host[:port]
      case "$rest" in *@*) user="${rest%%@*}"; rest="${rest#*@}" ;; esac
      host="${rest%%:*}"
      case "$rest" in *:*) port="${rest##*:}" ;; esac
      ;;
    *@*:*)   # scp-like user@host:path
      user="${url%%@*}"; rest="${url#*@}"; host="${rest%%:*}"
      ;;
  esac
  [ -n "$host" ] || host="$gh"
  [ -n "$port" ] || port="$gp"
  [ -n "$port" ] || port="29418"
  if [ -n "$GERRIT_USER" ]; then
    user="$GERRIT_USER"
  elif [ -z "$user" ]; then
    user="$(git -C "$root" config --get gitreview.username 2>/dev/null || true)"
  fi
  printf '%s %s\n' "${user:+$user@}$host" "$port"
}

# Ensure the Gerrit commit-msg hook is installed (mints Change-Ids).
ensure_commit_msg_hook() {
  local root="$1" target="$2" port="$3" hookdir hook
  hookdir="$(git -C "$root" rev-parse --git-path hooks 2>/dev/null || true)"
  [ -n "$hookdir" ] || return 1
  # rev-parse --git-path may print a path relative to the repo root.
  case "$hookdir" in
    /*) : ;;
    *) hookdir="$root/$hookdir" ;;
  esac
  hook="$hookdir/commit-msg"
  if [ -x "$hook" ]; then
    return 0
  fi
  mkdir -p "$hookdir" || return 1
  log "installing Gerrit commit-msg hook from $target"
  if scp -p -P "$port" -o BatchMode=yes -o "ConnectTimeout=$SSH_CONNECT_TIMEOUT" \
        "$target:hooks/commit-msg" "$hook" >/dev/null 2>&1; then
    chmod +x "$hook"
    if [ -x "$hook" ]; then
      return 0
    fi
  fi
  return 1
}

count_module_dirs() {
  local dir="$1"
  if [ -d "$dir" ]; then
    find "$dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d '[:space:]'
  else
    echo 0
  fi
}

# Refuse a tree with no modules visible at all (a blind analyser classifies
# live data as unused and deletes it). Consumers can come from the
# environment's own modules/ or from the global modules dir, so either being
# populated is enough. A Puppetfile with no modules anywhere is a blind tree.
deployment_ok() {
  local envroot="$1" code_dir="$2" envmods globmods
  envmods="$(count_module_dirs "$envroot/modules")"
  globmods="$(count_module_dirs "$code_dir/modules")"
  log "deployment check: env modules=$envmods, global modules=$globmods"
  if [ "$envmods" -gt 0 ] || [ "$globmods" -gt 0 ]; then
    return 0
  fi
  if [ -f "$envroot/Puppetfile" ]; then
    return 1
  fi
  warn "no modules found and no Puppetfile; cannot verify deployment, proceeding"
  return 0
}

# Install the environment's Puppetfile modules so they match the Puppetfile
# (right modules, right versions) before analysis. Without this hiera-gc may
# not see a key's consumer and would wrongly mark the key unused. A no-op when
# the environment has no Puppetfile or installs are disabled. The command runs
# with the environment as its working directory. When R10K_CONFIG is set it is
# passed as a global option (r10k --config FILE puppetfile install).
ensure_modules() {
  local envroot="$1" out ln
  local -a cmd
  [ "$PUPPETFILE_INSTALL" -eq 1 ] || return 0
  [ -f "$envroot/Puppetfile" ] || return 0
  cmd=( "${PF_CMD[@]}" )
  if [ -n "$R10K_CONFIG" ]; then
    cmd=( "${PF_CMD[0]}" --config "$R10K_CONFIG" "${PF_CMD[@]:1}" )
  fi
  out="$(mktemp "$TMPDIR_RUN/puppetfile.XXXXXX")"
  log "installing Puppetfile modules: ${cmd[*]} (in $envroot)"
  if ( cd "$envroot" && "${cmd[@]}" ) >"$out" 2>&1; then
    return 0
  fi
  err "Puppetfile install failed:"
  while IFS= read -r ln; do err "  $ln"; done < <(tail -n 20 "$out")
  return 1
}

# Read the Change-Ids from a gerrit query JSON blob. gerrit query --format=JSON
# prints one object per matching change (with an "id" Change-Id field and no
# "type" key), then a trailing summary row with "type":"stats". So we collect
# every row that has an "id" and is not the stats row.
parse_change_ids() {
  printf '%s\n' "$1" | python3 -c '
import sys, json
ids = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except ValueError:
        continue
    if obj.get("type") == "stats":
        continue
    cid = obj.get("id")
    if cid:
        ids.append(cid)
print("\n".join(ids))
'
}

# Count fixes.actions in the JSON report.
json_count_actions() {
  python3 -c '
import sys, json
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(0); raise SystemExit(0)
print(len((d.get("fixes") or {}).get("actions") or []))
' "$1"
}

# Exit non-zero if fixes.errors is non-empty.
json_errors_empty() {
  python3 -c '
import sys, json
d = json.load(open(sys.argv[1]))
errs = (d.get("fixes") or {}).get("errors") or []
raise SystemExit(1 if errs else 0)
' "$1"
}

# Render a value-free commit message (or a dry-run summary to stderr). File
# paths are shown relative to the environment root, and the message carries no
# environment name (the repo it lands in identifies the environment).
render_message() {
  # render_message REPORT ENVROOT MARKER_TOKEN OUTFILE|-
  python3 -c '
import sys, json, os
report, envroot, marker, out = sys.argv[1:5]
try:
    d = json.load(open(report))
except Exception:
    d = {}
fixes = d.get("fixes") or {}
actions = fixes.get("actions") or []
base = os.path.realpath(envroot)
def rel(p):
    try:
        return os.path.relpath(os.path.realpath(str(p)), base)
    except Exception:
        return str(p)
by_kind = {}
for a in actions:
    by_kind.setdefault(a.get("finding", "?"), []).append(a)
lines = []
lines.append("Remove unused/redundant/orphaned Hiera data")
lines.append("")
lines.append("Automated cleanup proposed by hiera-gc --fix.")
lines.append("This report names keys, files and line numbers only; no data values.")
lines.append("")
lines.append("Changes (%d):" % len(actions))
for kind in sorted(by_kind):
    items = by_kind[kind]
    lines.append("  %s: %d" % (kind, len(items)))
    for a in sorted(items, key=lambda x: (str(x.get("file")), str(x.get("key") or ""))):
        if a.get("action") == "delete_file":
            lines.append("    delete file %s" % rel(a.get("file")))
        else:
            lines.append("    remove key %s  [%s]" % (a.get("key"), rel(a.get("file"))))
skipped = fixes.get("skipped") or []
if skipped:
    lines.append("  skipped (not mechanically removable): %d" % len(skipped))
oos = fixes.get("out_of_scope") or {}
if oos:
    lines.append("  out of scope (shared/global/module/other env): "
                 + ", ".join("%s=%s" % (k, v) for k, v in sorted(oos.items())))
lines.append("")
lines.append("Review the diff and wait for the catalog-diff CI job before merging.")
lines.append("")
lines.append("%s: true" % marker)
text = "\n".join(lines) + "\n"
if out == "-":
    sys.stderr.write(text)
else:
    open(out, "w").write(text)
' "$1" "$2" "$3" "$4"
}

# Emit the changed paths (relative to the repo root) from git status, handling
# renames and quoted names.
porcelain_paths() {
  local root="$1" line path
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    path="${line:3}"
    case "$path" in
      *" -> "*) path="${path##* -> }" ;;
    esac
    path="${path#\"}"; path="${path%\"}"
    printf '%s\n' "$path"
  done < <(git -C "$root" status --porcelain)
}

# Read changed paths (relative to the repo root) on stdin and print only those
# the fix report says hiera-gc changed. Everything else in the work tree, such
# as modules installed by the Puppetfile step, is deployment noise and is
# deliberately dropped, so it is never staged or committed.
select_changed_in_report() {
  python3 -c '
import sys, os, json
root, report = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(report))
except Exception:
    d = {}
want = set()
for a in (d.get("fixes") or {}).get("actions") or []:
    f = a.get("file")
    if f:
        want.add(os.path.realpath(f))
for raw in sys.stdin:
    p = raw.rstrip("\n")
    if p and os.path.realpath(os.path.join(root, p)) in want:
        print(p)
' "$1" "$2"
}

# Restore a checkout to the clean branch tip after a refused/aborted attempt.
restore_tree() {
  local root="$1" ref="$2"
  git -C "$root" reset --hard --quiet "$ref" 2>/dev/null || true
  git -C "$root" clean -fd --quiet 2>/dev/null || true
}

# --------------------------------------------------------------------------
# Process one environment. Returns 0 on success (including a clean no-op),
# non-zero on any failure so the caller records it and moves on. errexit is
# ignored inside this function because it is called from an `if`, so each
# meaningful command is guarded explicitly.
# --------------------------------------------------------------------------
process_env() {
  local repo="$1" env_opt="$2" code_dir_opt="$3"
  local repo_root host port project defaultbranch branch gerrit_remote
  local env code_dir envroot ssh_target ssh_port
  local report msgfile nactions ln
  local raw reuse_id ids_raw idcount got
  local -a ids=() discover_args=() to_add=()

  # 1. Validate the checkout and locate the environment.
  if [ ! -d "$repo" ]; then err "repo dir not found: $repo"; return 1; fi
  if ! git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    err "not a git work tree: $repo"; return 1
  fi
  repo_root="$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -z "$repo_root" ]; then err "cannot resolve repo root for $repo"; return 1; fi

  # Two layouts are supported:
  #   A. the checkout is itself an environment (a hiera.yaml at its root). Its
  #      parent acts as an environments-root, so we pass --env-dir and take the
  #      environment name from the directory name.
  #   B. the checkout is a full code tree with environments/<env> inside it.
  if [ -f "$repo_root/hiera.yaml" ]; then
    env="$(basename "$repo_root")"
    if [ -n "$env_opt" ] && [ "$env_opt" != "$env" ]; then
      warn "config env '$env_opt' ignored; for this layout the environment name is the directory name '$env'"
    fi
    code_dir="${code_dir_opt:-$CODE_DIR_DEFAULT}"
    envroot="$repo_root"
    discover_args=( --code-dir "$code_dir" --env-dir "$(dirname "$repo_root")" --env "$env" )
  else
    if [ -z "$env_opt" ]; then
      err "no hiera.yaml in $repo_root and no env name given; cannot locate an environment"
      return 1
    fi
    env="$env_opt"
    code_dir="${code_dir_opt:-$repo_root}"
    envroot="$code_dir/environments/$env"
    discover_args=( --code-dir "$code_dir" --env "$env" )
  fi
  if [ ! -d "$envroot" ]; then
    err "environment not found at $envroot (check the repo/env/code_dir mapping)"; return 1
  fi
  case "$(readlink -f "$envroot")/" in
    "$(readlink -f "$repo_root")"/*) : ;;
    *) err "environment $envroot is not inside repo $repo_root"; return 1 ;;
  esac
  CUR_ENV="$env"

  # Dry-run short-circuit: analyse the modules currently on disk and report. No
  # git mutation, no network and no module install, so it is safe to run
  # against a working checkout. (A real run installs the Puppetfile modules
  # first; in dry-run we report on what is already deployed.)
  if [ "$DRY_RUN" -eq 1 ]; then
    if ! deployment_ok "$envroot" "$code_dir"; then
      err "no modules visible for $envroot; refusing to analyse a blind tree"; return 1
    fi
    report="$TMPDIR_RUN/report-$env.json"
    local -a dry_args=( "${discover_args[@]}" --fix --fix-kinds "$FIX_KINDS"
                        --fail-on none --format json --output "$report" --dry-run )
    [ -n "$ALLOWLIST" ] && dry_args+=( --allowlist "$ALLOWLIST" )
    local drc=0
    "${HG_CMD[@]}" "${dry_args[@]}" || drc=$?
    if [ "$drc" -ge 2 ]; then
      err "hiera-gc exited $drc (parse errors or refusal); skipping"
      return 1
    fi
    nactions="$(json_count_actions "$report")"
    log "[dry-run] would change $nactions item(s):"
    render_message "$report" "$envroot" "$MARKER_TOKEN" "-"
    ENTRY_STATE="dryrun"
    return 0
  fi

  # 2. Gerrit coordinates from .gitreview.
  if [ ! -f "$repo_root/.gitreview" ]; then err "no .gitreview in $repo_root"; return 1; fi
  host="$(git config -f "$repo_root/.gitreview" --get gerrit.host 2>/dev/null || true)"
  if [ -z "$host" ]; then err "no gerrit.host in $repo_root/.gitreview"; return 1; fi
  port="$(git config -f "$repo_root/.gitreview" --get gerrit.port 2>/dev/null || echo 29418)"
  project="$(git config -f "$repo_root/.gitreview" --get gerrit.project 2>/dev/null || true)"
  if [ -z "$project" ]; then err "no gerrit.project in $repo_root/.gitreview"; return 1; fi
  project="${project%.git}"
  defaultbranch="$(git config -f "$repo_root/.gitreview" --get gerrit.defaultbranch 2>/dev/null || echo master)"

  # 3. Resolve one target branch, reused for query, reset and push.
  branch="$(git -C "$repo_root" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
    branch="$defaultbranch"
    log "detached HEAD; using .gitreview defaultbranch '$branch'"
  elif [ "$branch" != "$defaultbranch" ]; then
    log "checked-out branch '$branch' differs from defaultbranch '$defaultbranch'; using '$branch'"
  fi
  if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then err "cannot resolve a target branch"; return 1; fi

  # 4. Gerrit remote and commit-msg hook (idempotent).
  git -C "$repo_root" review -s >/dev/null 2>&1 || warn "git review -s did not complete cleanly"
  gerrit_remote="$(resolve_gerrit_remote "$repo_root" "$host" || true)"
  if [ -z "$gerrit_remote" ]; then
    err "no git remote URL matches Gerrit host $host"; return 1
  fi
  IFS=' ' read -r ssh_target ssh_port < <(gerrit_ssh_coords "$repo_root" "$gerrit_remote" "$host" "$port")
  case "$ssh_target" in
    *@*) : ;;
    *) warn "no Gerrit ssh username resolved for $ssh_target; ssh will use the local user. Set 'git config --global gitreview.username NAME' or pass --gerrit-user" ;;
  esac
  if ! ensure_commit_msg_hook "$repo_root" "$ssh_target" "$ssh_port"; then
    err "Gerrit commit-msg hook is missing and could not be installed"; return 1
  fi
  if ! git -C "$repo_root" ls-remote --exit-code --heads "$gerrit_remote" "$branch" >/dev/null 2>&1; then
    err "branch '$branch' does not exist on remote '$gerrit_remote'"; return 1
  fi

  # 5. Refresh to the clean branch tip (unconditional; see header notes).
  if ! git -C "$repo_root" fetch --quiet "$gerrit_remote" "$branch"; then
    err "git fetch $gerrit_remote $branch failed"; return 1
  fi
  if [ "$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)" != \
       "$(git -C "$repo_root" rev-parse "$gerrit_remote/$branch" 2>/dev/null || true)" ] \
     || [ -n "$(git -C "$repo_root" status --porcelain)" ]; then
    log "tree differs from $gerrit_remote/$branch (dirty or ahead); resetting to tip"
  fi
  if ! git -C "$repo_root" reset --hard --quiet "$gerrit_remote/$branch"; then
    err "git reset --hard $gerrit_remote/$branch failed"; return 1
  fi
  git -C "$repo_root" clean -fd --quiet || { err "git clean failed"; return 1; }

  # 6. Install the env's Puppetfile modules so hiera-gc sees the real
  # consumers. The install may also change committed (tracked) files: r10k
  # purges moduledir content not in the Puppetfile, which deletes local modules
  # that live in the env's own git tree. Restore any tracked file the install
  # removed or modified, so the env's own modules are preserved and stay
  # visible to the analysis, while keeping the install's untracked modules.
  # This also leaves no unstaged tracked changes, which git review requires.
  if ! ensure_modules "$envroot"; then
    err "could not install Puppetfile modules; skipping (analysis would be blind)"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  fi
  git -C "$repo_root" checkout -- . >/dev/null 2>&1 || true
  if ! deployment_ok "$envroot" "$code_dir"; then
    err "no modules visible for $envroot after install; refusing to fix a blind tree"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  fi

  # 7. Apply the fix.
  report="$TMPDIR_RUN/report-$env.json"
  local -a fix_args=( "${discover_args[@]}" --fix --fix-kinds "$FIX_KINDS"
                      --fail-on none --format json --output "$report" )
  [ -n "$ALLOWLIST" ] && fix_args+=( --allowlist "$ALLOWLIST" )
  local rc=0
  "${HG_CMD[@]}" "${fix_args[@]}" || rc=$?
  if [ "$rc" -ge 2 ]; then
    err "hiera-gc exited $rc (refused due to parse errors, or a fix failed); restoring and skipping"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  fi

  # 8. Integrity and scope of the fix report.
  if ! json_errors_empty "$report"; then
    err "report records fix errors; aborting"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  fi
  nactions="$(json_count_actions "$report")"
  if [ "$nactions" -eq 0 ]; then
    log "no Hiera data changes to propose"
    ENTRY_STATE="nochange"
    return 0
  fi
  if [ "$MAX_REMOVALS" -gt 0 ] && [ "$nactions" -gt "$MAX_REMOVALS" ]; then
    err "would change $nactions item(s) > --max-removals $MAX_REMOVALS; skipping (possible blindness or drift)"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  fi

  # 9. Stage exactly the files hiera-gc reported changing. Any other work-tree
  # change (modules installed from the Puppetfile, or other noise) is ignored
  # and never committed.
  mapfile -t to_add < <(porcelain_paths "$repo_root" | select_changed_in_report "$repo_root" "$report")
  if [ "${#to_add[@]}" -eq 0 ]; then
    err "hiera-gc reported $nactions change(s) but none are visible in the work tree; aborting"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  fi

  # 11. Find an existing open review (only now that we have a diff to push).
  #     A failed query is a hard stop, never a fall-through to a duplicate.
  if ! raw="$(ssh -p "$ssh_port" -o BatchMode=yes -o "ConnectTimeout=$SSH_CONNECT_TIMEOUT" "$ssh_target" \
        gerrit query --format=JSON \
        "status:open" "project:$project" "branch:$branch" "topic:$TOPIC" "owner:self" "limit:11")"; then
    err "gerrit query failed; not creating a review (would risk a duplicate)"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  fi
  if ! ids_raw="$(parse_change_ids "$raw")"; then
    err "could not parse gerrit query output; aborting"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  fi
  if [ -n "$ids_raw" ]; then
    mapfile -t ids <<<"$ids_raw"
  fi
  reuse_id=""
  if [ "${#ids[@]}" -gt 1 ]; then
    err "found ${#ids[@]} open reviews for topic '$TOPIC' on '$branch'; skipping (ambiguous, resolve by hand)"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  elif [ "${#ids[@]}" -eq 1 ]; then
    reuse_id="${ids[0]}"
    if [[ ! "$reuse_id" =~ ^I[0-9a-f]{40}$ ]]; then
      err "open review Change-Id is malformed: '$reuse_id'; aborting"
      restore_tree "$repo_root" "$gerrit_remote/$branch"
      return 1
    fi
    log "found open review $reuse_id; will add a patchset"
  else
    log "no open review; will create a new one"
  fi

  # 12. Build the value-free commit message.
  msgfile="$TMPDIR_RUN/msg-$env.txt"
  render_message "$report" "$envroot" "$MARKER_TOKEN" "$msgfile"
  if [ -n "$reuse_id" ]; then
    if git -C "$repo_root" interpret-trailers --if-exists doNothing \
          --trailer "Change-Id: $reuse_id" "$msgfile" > "$msgfile.tmp"; then
      mv "$msgfile.tmp" "$msgfile"
    else
      err "could not insert reused Change-Id; aborting"
      rm -f "$msgfile.tmp"
      restore_tree "$repo_root" "$gerrit_remote/$branch"
      return 1
    fi
  fi

  # 13. Stage exactly the files the report accounted for, then commit. A
  # pre-commit hook on the env repo may rewrite the staged files for style and
  # abort the commit; re-stage and retry a few times so those fixups are picked
  # up. Re-staging only our own files keeps the commit scoped.
  local commit_ok=0 attempt commit_out
  for attempt in 1 2 3; do
    if ! git -C "$repo_root" add -- "${to_add[@]}"; then
      err "git add failed"; restore_tree "$repo_root" "$gerrit_remote/$branch"; return 1
    fi
    if commit_out="$(git -C "$repo_root" -c "user.name=$BOT_NAME" -c "user.email=$BOT_EMAIL" \
          commit -s -F "$msgfile" 2>&1)"; then
      commit_ok=1
      break
    fi
    log "commit attempt $attempt did not succeed (a pre-commit hook may have rewritten files); retrying"
  done
  if [ "$commit_ok" -ne 1 ]; then
    err "git commit failed after $attempt attempt(s):"
    while IFS= read -r ln; do err "  $ln"; done <<<"$commit_out"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  fi

  # 14. Verify exactly one Change-Id, and that a reuse kept the queried id.
  idcount="$(git -C "$repo_root" log -1 --format=%B \
             | git interpret-trailers --parse 2>/dev/null \
             | grep -c '^Change-Id:' || true)"
  if [ "$idcount" -ne 1 ]; then
    err "expected exactly one Change-Id, found $idcount; aborting before push"
    restore_tree "$repo_root" "$gerrit_remote/$branch"
    return 1
  fi
  if [ -n "$reuse_id" ]; then
    got="$(git -C "$repo_root" log -1 --format=%B \
           | git interpret-trailers --parse 2>/dev/null \
           | sed -n 's/^Change-Id: //p' | head -n1)"
    if [ "$got" != "$reuse_id" ]; then
      err "Change-Id mismatch (got '$got', expected '$reuse_id'); aborting before push"
      restore_tree "$repo_root" "$gerrit_remote/$branch"
      return 1
    fi
  fi

  # 15. Push for review (never auto-approve or submit).
  if [ "$NO_PUSH" -eq 1 ]; then
    log "committed locally; --no-push set, not pushing"
    ENTRY_STATE="committed"
    return 0
  fi
  local review_out review_rc=0
  review_out="$(git -C "$repo_root" review -y -t "$TOPIC" "$branch" 2>&1)" || review_rc=$?
  if [ "$review_rc" -ne 0 ]; then
    # Gerrit rejects a patchset identical to the current one with "no new
    # changes"; for us that means the open review already reflects this exact
    # cleanup, so there is nothing to do.
    if printf '%s\n' "$review_out" | grep -qiE "no new changes|no changes made"; then
      log "open review ${reuse_id:-} already reflects this cleanup (no new changes)"
      ENTRY_STATE="uptodate"
      return 0
    fi
    err "git review failed:"
    while IFS= read -r ln; do err "  $ln"; done <<<"$review_out"
    return 1
  fi
  if [ -n "$reuse_id" ]; then
    ENTRY_STATE="updated"
    log "updated review $reuse_id on '$branch' (topic '$TOPIC')"
  else
    ENTRY_STATE="created"
    log "created review on '$branch' (topic '$TOPIC')"
  fi
  return 0
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
print_summary() {
  CUR_ENV="main"
  log "summary:"
  local r
  for r in "${RESULTS[@]}"; do
    log "  $r"
  done
  log "done, exit $OVERALL_RC"
}

main() {
  parse_args "$@"
  setup
  acquire_lock
  load_entries
  # An explicit --r10k-config wins over the config file's r10k_config setting.
  [ -n "$R10K_CONFIG_CLI" ] && R10K_CONFIG="$R10K_CONFIG_CLI"
  if [ "${#ENTRY_REPO[@]}" -eq 0 ]; then
    log "no environments to process; nothing to do"
    exit 0
  fi
  [ "$DRY_RUN" -eq 1 ] && log "dry-run: no edits, commits or pushes will be made"
  local i
  for i in "${!ENTRY_REPO[@]}"; do
    # process_env resets CUR_ENV to the resolved environment name; until then
    # fall back to the configured env or the directory name for labelling.
    CUR_ENV="${ENTRY_ENV[$i]:-$(basename "${ENTRY_REPO[$i]}")}"
    ENTRY_STATE="failed"
    if process_env "${ENTRY_REPO[$i]}" "${ENTRY_ENV[$i]}" "${ENTRY_CODE[$i]}"; then
      :
    else
      OVERALL_RC=1
      ENTRY_STATE="failed"
    fi
    RESULTS+=("$CUR_ENV = $ENTRY_STATE")
  done
  print_summary
  exit "$OVERALL_RC"
}

main "$@"
