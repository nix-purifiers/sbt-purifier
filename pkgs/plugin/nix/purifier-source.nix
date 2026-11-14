# The source of the purifier library, at the revision that the flake.lock
# of this sbt-purifier pins. In the repository, the flake.lock is at the
# root; the plugin jar carries a copy of it beside the Nix library.
let
  lockFile = ../../../flake.lock;
in
builtins.fetchTree (builtins.fromJSON (builtins.readFile lockFile)).nodes.purifier.locked
