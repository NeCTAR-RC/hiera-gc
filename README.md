# hiera-gc

Find unused, redundant and orphaned Hiera data in a deployed Puppet code
tree. Static analysis only: it runs fully offline against
`/etc/puppetlabs/code` (or a copy) and reports key names, file paths and
line numbers. Data values are never printed and eyaml values are never
decrypted.

Documentation will grow with the implementation.
