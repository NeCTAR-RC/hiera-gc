# hiera-gc

Find unused, redundant and orphaned Hiera data in a deployed Puppet
code tree.

`hiera-gc` is a static analyser. It runs fully offline against
`/etc/puppetlabs/code` (or a copy of it) and reports:

- **Unused keys**: data keys with no visible consumer. Consumers it
  understands: automatic parameter lookup (class parameters),
  `lookup()` / `hiera*()` / `Deferred('lookup', ...)` calls in
  manifests, EPP and ERB templates and Ruby plugins, and
  `%{lookup(...)}` / `%{alias(...)}` interpolation inside data itself.
- **Possibly used keys**: keys it cannot prove unused, with the reason
  (a dynamic `lookup("${var}::key")` pattern match, a `lookup_options`
  reference, a `scope[...]` variable read, or the key name appearing
  verbatim somewhere).
- **Stale parameters**: keys shaped `class::param` where the class
  exists but the parameter does not. Strong removal candidates.
- **Redundant overrides**: a key re-defined at a higher hierarchy
  level with the same value it would resolve to anyway. The report
  names the copy to remove. Keys consumed by merging lookups
  (`hiera_hash`, `hiera_array`, deep `lookup_options`) are excluded.
- **Shadowed definitions**: a definition that can never win because an
  always-loaded higher-priority level defines a different value.
  Usually a latent bug.
- **Orphaned data files**: files no hierarchy path or glob can ever
  load (including `.yml` vs `.yaml` traps).
- **Stale data files**: group files (`nodegroups/<x>.yaml` etc.) whose
  hierarchy variable never takes that value in any manifest selector,
  and `nodes/<fqdn>.yaml` files matching no node definition (e.g.
  decommissioned hosts). Skipped when the evidence is not static.

## Safety

The tool never prints data values: reports contain key names, file
paths, line numbers and reason descriptions only, so the report itself
is safe to share. eyaml `ENC[...]` values (GPG or PKCS7, in any file
extension) are treated as opaque; no decryption keys are needed or
used. Value comparisons for redundancy detection use SHA-256 digests.
A test suite canary asserts no value can reach the output.

## Installation

Requires Python >= 3.10 and PyYAML. Either:

```
pip install .
```

or build a self-contained zipapp (PyYAML bundled) to copy onto a
puppetserver:

```
make zipapp
scp dist/hiera-gc puppet:/tmp/
ssh puppet python3 /tmp/hiera-gc --stats
```

## Usage

```
hiera-gc [--code-dir /etc/puppetlabs/code] \
         [--global-hiera /etc/puppetlabs/puppet/hiera.yaml] \
         [--env production ...] [--env-glob 'prod*'] \
         [--env-dir /data/extra-envs ...] \
         [--format text|json] [--output report.txt] \
         [--show unused,possibly_used,redundant,...] \
         [--fail-on unused,redundant] \
         [--allowlist allow.txt] [--extra-datadir PATH] \
         [--fix --env production] [--fix-kinds unused,redundant,...] [--dry-run] \
         [--strict] [--stats] [-v]
```

- The tool reads the deployed tree: environments under
  `<code-dir>/environments`, global modules at `<code-dir>/modules`,
  plus any datadir referenced by hiera.yaml files (absolute datadirs
  such as `/etc/puppetlabs/code/hieradata` are rebased under
  `--code-dir` when analysing a copied tree).
- `--env-dir PATH` adds another environments-root directory to search,
  like an extra entry on Puppet's `environmentpath`. Each root holds
  environment subdirectories; the default `<code-dir>/environments` is
  searched first, then each `--env-dir` in order. If the same
  environment name appears under more than one root the first wins (the
  rest are reported as shadowed), matching Puppet. `--env` and
  `--env-glob` filter across all roots.
- When `--env` or `--env-glob` narrows the run to a single environment,
  the report (and the exit status) covers only that environment's own
  files. Findings and warnings about shared, global or module data are
  visible to other environments the run did not analyse, so they are
  unreliable here as well as not fixable from this environment; they are
  listed by an all-environments run (no `--env` filter, or one matching
  more than one environment) instead. Warnings about files outside the
  environment's own tree (a module's hiera.yaml, a `lookup()` in a module
  manifest) are dropped the same way, except parse errors, which are kept
  because a file that fails to parse blinds the analysis. The report
  names the environment and how many findings it hid. This matches the
  scope of `--fix`.
- `environment.conf` modulepath entries (e.g.
  `site:modules:$basemodulepath`) are honoured.
- Exit codes: 0 clean, 1 findings matched `--fail-on`, 2 usage or
  (with `--strict`) parse errors. Diagnostics go to stderr; the report
  goes to stdout.

The allowlist file holds one Python regex per line (`#` comments);
keys whose full name matches are reported separately and never fail
the run. Use it for keys consumed by systems the analyser cannot see.
Allowlisted keys are never fixed.

## Fixing findings

`--fix` removes fixable findings from the one environment named by
`--env`. It acts on exactly one environment per run, so `--env` is
mandatory and must name a single environment (`--fix` will not fix
every environment at once, and cannot be combined with `--env-glob`):

```
hiera-gc --fix --env production --dry-run        # show what would change
hiera-gc --fix --env production --fail-on none   # apply it
```

- Only data files inside that environment's own directory are
  touched, so each run yields one reviewable commit in one repo.
  Findings in shared, global or module layer data (visible to other
  environments, and module data is usually vendored by r10k) are
  listed as out of scope; run `--fix --env NAME` again per environment
  for the rest.
- `--fix-kinds` selects what to fix: `unused`, `stale_params` (just
  the stale-parameter subset of unused), `redundant` (removes the
  higher-priority copy), `orphans` and `stale_files` (deletes the
  file). Default: `unused,redundant,orphans,stale_files`. Shadowed
  definitions are never auto-fixed; they are likely bugs and the
  right fix is ambiguous.
- Key removals are line-based edits driven by the parsed YAML node
  positions: comments, ordering, anchors and eyaml `ENC[...]` values
  elsewhere in the file are preserved. Definitions that are not safe
  to cut out mechanically are skipped with a reason: values anchored
  and aliased by another key, duplicate top-level keys, keys
  introduced via YAML merge, flow-style root mappings (JSON) and
  files without line information.
- With any parse errors in the run, `--fix` refuses to act: a file
  the analyser could not read may hold the only consumer of a key.
- Exit codes are unchanged, so a fix run usually wants
  `--fail-on none`. Run against a clean checkout and review the diff
  before pushing; the tool does not keep backups.

## Before you delete anything

Treat the report as a list of removal candidates, not a verdict:

- Keys may be consumed by things outside the code tree: cron jobs
  running `puppet lookup`, monitoring scripts, other repositories.
  Grep your ops repos before deleting.
- `lookup($variable)` calls with runtime-built keys are reported as
  warnings; keys they consume cannot be detected.
- A key reported as used via automatic parameter lookup only proves
  the class and parameter exist, not that the class is ever included
  on a node.

Remove data in small batches and watch catalog compilation (e.g. an
r10k catalog-diff run) before merging.

## Periodic automated cleanup

`scripts/hiera-gc-autofix.sh` runs `--fix` over a set of Puppet
environment git checkouts and proposes the cleanup to Gerrit (via
`git review`), unattended and on a schedule. It is built to be run from
cron and to be idempotent: if an automated review for an environment is
still open (not merged), the next run reuses that change's Change-Id so
Gerrit records a new patchset instead of opening a second review. It
finds the open review with a live `gerrit query` (`status:open` plus a
fixed topic), so a review that has merged or been abandoned is never
mistaken for a current one.

The commit message is value-free and carries no environment name (the
repo it lands in identifies the environment); file paths are shown
relative to the environment root.

Before analysing an environment that has a `Puppetfile`, it installs the
modules that Puppetfile declares (by default `r10k puppetfile install`,
run in the environment), so the modules are present and at the right
version and hiera-gc can see every consumer. The install command is
configurable (`--puppetfile-cmd`) and can be turned off
(`--no-puppetfile-install`). Modules it installs are never staged into
the commit (the script commits only the files hiera-gc reports
changing), so the managed module directory does not need to be
gitignored. If the install purges a local module committed in the
environment's own tree (r10k removes moduledir content not in the
Puppetfile), the script restores it, so the environment's own modules
are preserved and stay visible to the analysis. `--dry-run` skips this
install and analyses the modules already on disk. To point r10k at a
specific `r10k.yaml`, set `r10k_config = /path/to/r10k.yaml` in the
config file (or pass `--r10k-config`); it is passed as `r10k --config
<file> puppetfile install`.

It pushes for review only and never approves or submits, so the human
plus catalog-diff review described above stays the gate before anything
merges. Other guard rails: it refuses to fix a checkout whose modules
are not deployed (a blind analysis would delete live data), stages only
the files `--fix` changed, retries the commit when the environment's
`pre-commit` hook rewrites files for style, and isolates its own reviews
by topic and a marker trailer so it coexists with other tooling on the
same repos.

List the environments in a config file (see
`scripts/hiera-gc-autofix.conf.example`), one per line. The script
auto-detects the layout of each checkout. A checkout that is itself an
environment (a `hiera.yaml` at its root, as r10k dev environments are)
needs only its path: the environment name is the directory name, its
parent is used as the environments root (`--env-dir`), and `--code-dir`
defaults to `/etc/puppetlabs/code` for global modules and shared data. A
full code tree instead takes the env name and looks under
`environments/<env>`:

```
# self-environment checkouts (env = directory name)
/etc/puppetlabs/code/dev_environments/sam_ardctest_default
/etc/puppetlabs/code/dev_environments/sam_rctest_default
# a full code tree
/srv/puppet-autofix/control-repo   production
```

These should be deployed automation clones (modules resolved into
`modules/`, or present in the global modules dir), used only by this
job, since each run hard-resets them to the Gerrit branch tip. Try a
single environment first:

```
hiera-gc-autofix.sh --dry-run /etc/puppetlabs/code/dev_environments/sam_ardctest_default
hiera-gc-autofix.sh --no-push /etc/puppetlabs/code/dev_environments/sam_ardctest_default
```

`--dry-run` reports what would change and makes no edits; `--no-push`
applies and commits locally but does not push. A typical cron entry runs
as the bot account whose ssh key reaches Gerrit:

```
17 6 * * *  /usr/local/bin/hiera-gc-autofix.sh --config /etc/hiera-gc-autofix/envs.conf --log-dir /var/log/hiera-gc-autofix
```

Because `unused` keys may be consumed by systems the analyser cannot
see, supply an `--allowlist` (see
`scripts/hiera-gc-autofix.allowlist.example`). Run
`hiera-gc-autofix.sh --help` for all options.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/pytest
tox          # full matrix
```
