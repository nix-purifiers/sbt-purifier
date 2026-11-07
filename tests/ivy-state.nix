{
  source ? ../.,
  system ? builtins.currentSystem,
}:
let
  f = builtins.getFlake (toString source);
  pkgs = import f.inputs.nixpkgs {
    inherit system;
    overlays = [ f.overlays.default ];
  };
  sdk = pkgs.sbt-purifier.buildSbtProject { src = source + /tests/fixtures/multi; };
in
pkgs.runCommand "sbt-ivy-state"
  {
    nativeBuildInputs = [
      pkgs.python3
      sdk.jdk
    ];
    SDK_CACHE = sdk.dependencies;
    SDK_LAUNCHER = sdk.lock.launcher;
  }
  ''
    export HOME="$TMPDIR/home"
    mkdir -p "$HOME"
    python3 ${./ivy-state.py} "$out"
  ''
