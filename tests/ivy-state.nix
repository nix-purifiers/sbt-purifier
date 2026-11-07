{
  source ? ../.,
  system ? builtins.currentSystem,
  pkgs ?
    let
      f = builtins.getFlake (toString source);
    in
    import f.inputs.nixpkgs {
      inherit system;
      overlays = [ f.overlays.default ];
    },
  sdk ? pkgs.sbt-purifier.buildSbtProject { src = source + /tests/fixtures/multi; },
}:
assert sdk.lock.sbtVersion == "1.13.1" && sdk.lock.javaVersion == "21";
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
