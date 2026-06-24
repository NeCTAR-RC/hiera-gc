#!/usr/bin/env bash
#
# Smoke test for scripts/hiera-gc-autofix.sh.
#
# It does not need a live Gerrit. It builds a real local git repo, points a
# "gerrit" remote at a bare repo (via url.insteadOf so the host string still
# matches), and shims ssh, scp, git-review and hiera-gc so the script's
# decision logic runs end to end. It then asserts the Change-Id idempotency
# behaviour: no open review creates a fresh change; one open review reuses its
# Change-Id; many open reviews abort; an ssh failure aborts without pushing;
# a no-op makes no commit; and --dry-run edits nothing.
#
# Run: bash tests/autofix_smoke.sh

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../scripts/hiera-gc-autofix.sh"

HOST="review.example.test"
PORT="29418"
PROJECT="Test/puppet"
TOPIC="hiera-gc"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/autofix-smoke.XXXXXX")"
SHIM="$WORK/shim"
# The bare repo lives under a directory named after the host, so the gerrit
# remote URL is a real local path that still contains the host string (which
# is how the script resolves which remote is Gerrit).
BARE="$WORK/$HOST/bare.git"
REPO="$WORK/repo"
GITREVIEW_LOG="$WORK/git-review.log"
mkdir -p "$SHIM" "$WORK/$HOST"

FAILURES=0
pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

# shellcheck disable=SC2329  # invoked via the EXIT trap below
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# Quiet, identity-stable git for the test.
export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.test"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.test"

# --------------------------------------------------------------------------
# Shims
# --------------------------------------------------------------------------
cat > "$SHIM/ssh" <<'EOF'
#!/usr/bin/env bash
# Fake gerrit query. FAKE_QUERY_FAIL=1 simulates an unreachable server.
# FAKE_OPEN_IDS holds space-separated open Change-Ids to report.
if [ "${FAKE_QUERY_FAIL:-0}" = "1" ]; then
  echo "ssh: connect: timed out" >&2
  exit 255
fi
# Mimic real `gerrit query --format=JSON`: change rows carry an "id"
# (the Change-Id) and NO "type" field; only the trailing stats row has a type.
n=0
for id in ${FAKE_OPEN_IDS:-}; do
  n=$((n + 1))
  printf '{"project":"Test/puppet","branch":"master","id":"%s","number":%d,"status":"NEW","owner":{"username":"bot"}}\n' "$id" "$n"
done
printf '{"type":"stats","rowCount":%d,"runTimeMilliseconds":1}\n' "$n"
exit 0
EOF

cat > "$SHIM/scp" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat > "$SHIM/git-review" <<'EOF'
#!/usr/bin/env bash
# -s is setup (no-op here). Anything else is a push; record it.
if [ "${1:-}" = "-s" ]; then
  exit 0
fi
printf 'PUSH %s\n' "$*" >> "$GITREVIEW_LOG"
exit 0
EOF

# Fake hiera-gc. FAKE_HG_MODE: edit (default) | noop | refuse.
cat > "$SHIM/hiera-gc" <<'EOF'
#!/usr/bin/env bash
set -u
code_dir=""; env=""; env_dir=""; output=""; dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --code-dir) code_dir="$2"; shift 2 ;;
    --env) env="$2"; shift 2 ;;
    --env-dir) env_dir="$2"; shift 2 ;;
    --output) output="$2"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    *) shift ;;
  esac
done
mode="${FAKE_HG_MODE:-edit}"
if [ "$mode" = "refuse" ]; then
  echo "hiera-gc: refusing to fix: parse error(s)" >&2
  exit 2
fi
# In the self-environment layout the env lives under --env-dir; otherwise under
# --code-dir/environments.
if [ -n "$env_dir" ]; then
  data="$env_dir/$env/data/common.yaml"
else
  data="$code_dir/environments/$env/data/common.yaml"
fi
actions="[]"
if [ "$mode" = "edit" ]; then
  if [ "$dry" -eq 0 ] && [ -f "$data" ]; then
    grep -v '^dead_key:' "$data" > "$data.tmp" && mv "$data.tmp" "$data"
  fi
  actions=$(printf '{"action":"remove_key","finding":"unused","file":"%s","key":"dead_key","start_line":2,"end_line":2}' "$data")
  actions="[$actions]"
fi
if [ -n "$output" ]; then
  cat > "$output" <<JSON
{"schema_version":1,"code_dir":"$code_dir","fixes":{"environment":"$env","dry_run":$([ "$dry" -eq 1 ] && echo true || echo false),"kinds":["unused"],"actions":$actions,"skipped":[],"out_of_scope":{},"errors":[]}}
JSON
fi
exit 0
EOF

chmod +x "$SHIM"/*
export GITREVIEW_LOG

# Gerrit commit-msg hook: mint a Change-Id when none is present (like Gerrit).
HOOK_SRC="$WORK/commit-msg"
cat > "$HOOK_SRC" <<'EOF'
#!/usr/bin/env bash
f="$1"
grep -q '^Change-Id:' "$f" && exit 0
id="I$( (date +%s%N; cat "$f") | sha1sum | cut -c1-40 )"
printf 'Change-Id: %s\n' "$id" >> "$f"
exit 0
EOF
chmod +x "$HOOK_SRC"

# --------------------------------------------------------------------------
# Repo + bare "gerrit" remote
# --------------------------------------------------------------------------
git init -q --bare "$BARE"
git init -q -b master "$REPO"
mkdir -p "$REPO/environments/production/data"
cat > "$REPO/environments/production/data/common.yaml" <<'EOF'
live_key: keep me
dead_key: remove me
EOF
cat > "$REPO/.gitreview" <<EOF
[gerrit]
host=$HOST
port=$PORT
project=$PROJECT.git
defaultbranch=master
EOF
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "initial"
# Remote URL is the local bare path, which contains the host string.
git -C "$REPO" remote add gerrit "$BARE"
git -C "$REPO" push -q gerrit master
cp "$HOOK_SRC" "$REPO/.git/hooks/commit-msg"
chmod +x "$REPO/.git/hooks/commit-msg"

# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
LOG="$WORK/run.log"
# These globals select which checkout the current scenario drives.
REPOCUR="$REPO"          # git checkout to inspect
ENVLABEL="production"    # env name expected in the summary
TARGET="$REPO:production" # entry argument passed to the script

run_script() {
  : > "$GITREVIEW_LOG"
  PATH="$SHIM:$PATH" \
    bash "$SCRIPT" \
      --hiera-gc "$SHIM/hiera-gc" \
      --lock "$WORK/lock" \
      --topic "$TOPIC" \
      --code-dir-default "$WORK/globalcode" \
      "$@" "$TARGET" >"$LOG" 2>&1
  echo $?
}
state_line() { grep -E "$ENVLABEL = " "$LOG" | tail -n1; }
committed_changeid() {
  git -C "$REPOCUR" log -1 --format=%B | sed -n 's/^Change-Id: //p' | head -n1
}
pushed() { grep -q '^PUSH' "$GITREVIEW_LOG"; }
head_is_tip() {
  [ "$(git -C "$REPOCUR" rev-parse HEAD)" = "$(git -C "$REPOCUR" rev-parse gerrit/master)" ]
}

# A fake global code dir with a module, so the deployment gate is satisfied
# even when an environment ships no modules of its own.
mkdir -p "$WORK/globalcode/modules/stdlib"

# --------------------------------------------------------------------------
# Scenario 1: no open review -> create with a fresh Change-Id
# --------------------------------------------------------------------------
rc=$(FAKE_OPEN_IDS="" FAKE_HG_MODE="edit" run_script)
if [ "$rc" = "0" ] && state_line | grep -q "created" && pushed; then
  cid="$(committed_changeid)"
  if [[ "$cid" =~ ^I[0-9a-f]{40}$ ]]; then
    pass "no open review creates a new review with a minted Change-Id ($cid)"
  else
    fail "create: Change-Id not minted (got '$cid')"
  fi
else
  fail "create: rc=$rc state='$(state_line)' pushed=$(pushed && echo yes || echo no)"
fi

# --------------------------------------------------------------------------
# Scenario 2: one open review -> reuse its Change-Id
# --------------------------------------------------------------------------
REUSE="I0123456789abcdef0123456789abcdef01234567"
rc=$(FAKE_OPEN_IDS="$REUSE" FAKE_HG_MODE="edit" run_script)
if [ "$rc" = "0" ] && state_line | grep -q "updated" && pushed; then
  cid="$(committed_changeid)"
  if [ "$cid" = "$REUSE" ]; then
    pass "one open review reuses its Change-Id ($cid)"
  else
    fail "update: Change-Id not reused (got '$cid', expected '$REUSE')"
  fi
else
  fail "update: rc=$rc state='$(state_line)' pushed=$(pushed && echo yes || echo no)"
fi

# --------------------------------------------------------------------------
# Scenario 3: many open reviews -> abort, do not push
# --------------------------------------------------------------------------
rc=$(FAKE_OPEN_IDS="$REUSE Iaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" FAKE_HG_MODE="edit" run_script)
if [ "$rc" = "1" ] && state_line | grep -q "failed" && ! pushed && head_is_tip; then
  pass "multiple open reviews abort without pushing"
else
  fail "ambiguous: rc=$rc state='$(state_line)' pushed=$(pushed && echo yes || echo no) head_is_tip=$(head_is_tip && echo yes || echo no)"
fi

# --------------------------------------------------------------------------
# Scenario 4: ssh query failure -> abort without pushing, tree restored
# --------------------------------------------------------------------------
rc=$(FAKE_QUERY_FAIL=1 FAKE_HG_MODE="edit" run_script)
if [ "$rc" = "1" ] && state_line | grep -q "failed" && ! pushed && head_is_tip; then
  pass "ssh query failure aborts without creating a duplicate review"
else
  fail "ssh-fail: rc=$rc state='$(state_line)' pushed=$(pushed && echo yes || echo no) head_is_tip=$(head_is_tip && echo yes || echo no)"
fi

# --------------------------------------------------------------------------
# Scenario 5: no changes -> no commit, no push
# --------------------------------------------------------------------------
rc=$(FAKE_HG_MODE="noop" run_script)
if [ "$rc" = "0" ] && state_line | grep -q "nochange" && ! pushed && head_is_tip; then
  pass "a no-op run proposes nothing"
else
  fail "noop: rc=$rc state='$(state_line)' pushed=$(pushed && echo yes || echo no) head_is_tip=$(head_is_tip && echo yes || echo no)"
fi

# --------------------------------------------------------------------------
# Scenario 6: --dry-run -> no edit, no commit, no push
# --------------------------------------------------------------------------
rc=$(FAKE_HG_MODE="edit" run_script --dry-run)
still_has_dead_key=$(grep -c '^dead_key:' "$REPO/environments/production/data/common.yaml")
if [ "$rc" = "0" ] && state_line | grep -q "dryrun" && ! pushed && head_is_tip && [ "$still_has_dead_key" -ge 1 ]; then
  pass "dry-run reports without editing, committing or pushing"
else
  fail "dry-run: rc=$rc state='$(state_line)' pushed=$(pushed && echo yes || echo no) dead_key_present=$still_has_dead_key"
fi

# --------------------------------------------------------------------------
# Scenario 7: self-environment layout (the checkout IS the environment).
# The env name is auto-derived from the directory and --env-dir is used.
# --------------------------------------------------------------------------
ENVPARENT="$WORK/dev_environments"
ENVCHK="$ENVPARENT/myenv"
ENVBARE="$WORK/$HOST/myenv-bare.git"
mkdir -p "$ENVCHK/data" "$ENVCHK/modules/mymod" "$ENVPARENT"
git init -q --bare "$ENVBARE"
git init -q -b master "$ENVCHK"
cat > "$ENVCHK/hiera.yaml" <<'EOF'
---
version: 5
defaults:
  datadir: data
  data_hash: yaml_data
hierarchy:
  - name: base
    path: common.yaml
EOF
printf 'live_key: keep me\ndead_key: remove me\n' > "$ENVCHK/data/common.yaml"
printf "mod 'mymod'\n" > "$ENVCHK/Puppetfile"
cat > "$ENVCHK/.gitreview" <<EOF
[gerrit]
host=$HOST
port=$PORT
project=$PROJECT.git
defaultbranch=master
EOF
git -C "$ENVCHK" add -A
git -C "$ENVCHK" commit -q -m "initial"
git -C "$ENVCHK" remote add gerrit "$ENVBARE"
git -C "$ENVCHK" push -q gerrit master
cp "$HOOK_SRC" "$ENVCHK/.git/hooks/commit-msg"
chmod +x "$ENVCHK/.git/hooks/commit-msg"

REPOCUR="$ENVCHK"; ENVLABEL="myenv"; TARGET="$ENVCHK"
rc=$(FAKE_OPEN_IDS="" FAKE_HG_MODE="edit" run_script)
dead_gone=0
grep -q '^dead_key:' "$ENVCHK/data/common.yaml" || dead_gone=1
subj="$(git -C "$ENVCHK" log -1 --format=%s)"
msg="$(git -C "$ENVCHK" log -1 --format=%B)"
if [ "$rc" = "0" ] && state_line | grep -q "myenv = created" && pushed && [ "$dead_gone" = "1" ]; then
  cid="$(committed_changeid)"
  if [[ "$cid" =~ ^I[0-9a-f]{40}$ ]]; then
    pass "self-environment layout: env auto-derived, --env-dir used, review created ($cid)"
  else
    fail "case-A: Change-Id not minted (got '$cid')"
  fi
else
  fail "case-A: rc=$rc state='$(state_line)' pushed=$(pushed && echo yes || echo no) dead_gone=$dead_gone"
fi

# Scenario 8: the commit message carries no environment name, and file paths
# are relative to the environment root (e.g. data/common.yaml, not the full
# checkout path).
relpath_ok=0; noenv_ok=0
printf '%s' "$msg" | grep -qF '[data/common.yaml]' && relpath_ok=1
[ "$subj" = "Remove unused/redundant/orphaned Hiera data" ] && noenv_ok=1
if [ "$relpath_ok" = "1" ] && [ "$noenv_ok" = "1" ]; then
  pass "commit message omits the environment name and uses env-relative paths"
else
  fail "case-A message: subj='$subj' relpath_ok=$relpath_ok noenv_ok=$noenv_ok"
fi

# --------------------------------------------------------------------------
echo
if [ "$FAILURES" -eq 0 ]; then
  echo "All smoke tests passed."
  exit 0
fi
echo "$FAILURES smoke test(s) failed."
exit 1
