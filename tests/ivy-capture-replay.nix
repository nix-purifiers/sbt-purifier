{ producerSource, projectSource, lockFile }:
let
  f = builtins.getFlake (toString producerSource);
  pkgs = import f.inputs.nixpkgs {
    system = "x86_64-linux";
    overlays = [ f.overlays.default ];
  };
  build = pkgs.sbt-purifier.buildSbtProject {
    src = projectSource;
    inherit lockFile;
    doCheck = true;
  };
in {
  inherit (build) tests dependencies;
  app = build.packages.app;
}
