{
  producerSource,
  projectSource,
  lockFile,
}:
let
  f = builtins.getFlake (toString producerSource);
  pkgs = import f.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ f.overlays.default ];
  };
  build = pkgs.sbt-purifier.buildSbtProject {
    src = projectSource;
    inherit lockFile;
    doCheck = true;
  };
in
{
  inherit (build) tests dependencies;
  app = build.packages.app;
}
