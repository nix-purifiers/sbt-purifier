# Installed entry point; preserve custom flat libDir overrides.
{
  lockFile,
  libDir ? ./.,
}:
import ./pkgs/plugin/nix/realize.nix {
  inherit lockFile;
  libDir = if toString libDir == toString ./. then null else libDir;
}
