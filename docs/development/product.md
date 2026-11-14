# Product

sbt-nix-backend makes Nix the dependency and build backend of sbt builds:
a build's downloads are locked by URL and hash, its packages build in the
Nix sandbox, and interactive sbt resolves from the same locked downloads
through the store. The README describes what it does for a user.

## What it owns

- The sbt plugin (`src/main/scala`): the lock run, the sandbox command,
  interactive mode, flake repositories, and the keys.
- The public Nix library (`lib-overlays/default`) and package helpers (`pkgs`), whose builder the flake overlay adds to a
  package set as `buildSbtProject`, for `callPackage`. The plugin jar carries the same
  files, so the code that builds a lockfile always comes from the plugin
  revision that wrote it.
- The lockfile format (`Lock.scala`, `lib-overlays/default/read-lock.nix`), versioned by its
  `version` field.
- The `sbt` wrapper (`pkgs/sbt`), which loads the plugin as a global
  plugin and boots sbt from the lock.

## Principles

- **A lock is complete by construction.** It is what a fresh-cache run of
  the sandboxed build downloaded, rather than a list of what sbt is known
  to fetch.
- **The plugin sits below every level of the build.** Delivered as a
  global plugin, it governs the resolution of each meta-build as well as
  the build.
- **One copy of the logic.** The build logic lives in the Nix library;
  sbt writes only the lockfile.

## Not in scope

A repository that needs credentials works only through a fetcher the
caller supplies.
