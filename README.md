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

Requires Python >= 3.8 and PyYAML. Either:

```
pip install .
```

or build a self-contained zipapp (PyYAML bundled) to copy onto a
puppetserver:

```
make zipapp
scp dist/hiera-gc.pyz puppet:/tmp/
ssh puppet python3 /tmp/hiera-gc.pyz --stats
```

## Usage

```
hiera-gc [--code-dir /etc/puppetlabs/code] \
         [--global-hiera /etc/puppetlabs/puppet/hiera.yaml] \
         [--env production ...] [--env-glob 'prod*'] \
         [--format text|json] [--output report.txt] \
         [--show unused,possibly_used,redundant,...] \
         [--fail-on unused,redundant] \
         [--allowlist allow.txt] [--extra-datadir PATH] \
         [--fix ENV] [--fix-kinds unused,redundant,...] [--dry-run] \
         [--strict] [--stats] [-v]
```

- The tool reads the deployed tree: environments under
  `<code-dir>/environments`, global modules at `<code-dir>/modules`,
  plus any datadir referenced by hiera.yaml files (absolute datadirs
  such as `/etc/puppetlabs/code/hieradata` are rebased under
  `--code-dir` when analysing a copied tree).
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

`--fix ENV` removes fixable findings, restricted to exactly one
environment per run:

```
hiera-gc --fix production --dry-run        # show what would change
hiera-gc --fix production --fail-on none   # apply it
```

- Only data files inside that environment's own directory are
  touched, so each run yields one reviewable commit in one repo.
  Findings in shared, global or module layer data (visible to other
  environments, and module data is usually vendored by r10k) and in
  other environments are listed as out of scope; run `--fix` again
  per environment for the rest.
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

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/pytest
tox          # full matrix
```
