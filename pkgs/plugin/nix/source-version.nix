# Source identity for the plugin, shared with project/SourceVersion.scala.
# Include the dependency lock: the self-build lock omits publication versions.
root:
let
  parts = [
    "build.sbt"
    "flake.lock"
    "project"
    "src"
    "lib-overlays"
    "pkgs"
    "tools"
    "sbt-purifier.lock.json"
  ];
  files =
    relative:
    let
      entries = builtins.readDir (root + "/${relative}");
    in
    builtins.concatLists (
      map (
        name:
        let
          path = if relative == "" then name else "${relative}/${name}";
          type = entries.${name};
        in
        if type == "directory" then
          if name == "target" then [ ] else files path
        else if type == "regular" then
          [ path ]
        else
          throw "sbt-purifier: unsupported source entry ${path} (${type})"
      ) (builtins.attrNames entries)
    );
  paths = builtins.sort builtins.lessThan (
    builtins.concatLists (
      map (
        part:
        if builtins.pathExists (root + "/${part}") then
          if (builtins.readDir root).${part} == "directory" then files part else [ part ]
        else
          throw "sbt-purifier: missing source version input ${part}"
      ) parts
    )
  );
  manifest = builtins.concatStringsSep "" (
    map (path: "${builtins.hashFile "sha256" (root + "/${path}")}  ${path}\n") paths
  );
in
{
  inherit manifest paths;
  version = "0.0.0-dev.${builtins.hashString "sha256" manifest}";
}
