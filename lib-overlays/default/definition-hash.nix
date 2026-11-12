# The definition hash of an sbt build at `root`: the digest the plugin
# records as `definitionHash` (Lock.buildDefinitionHash). It is the SHA-256
# of one line `<path> <SHA-256 of the file>` for each `*.sbt` file at the
# root and each file under `project/`, in name order at each level, leaving
# out build outputs and editor state. It needs no nixpkgs.
root:
let
  inherit (builtins)
    attrNames
    concatLists
    concatStringsSep
    elem
    filter
    hashFile
    hashString
    pathExists
    readDir
    stringLength
    substring
    ;

  outsideTheBuild =
    name: type:
    (
      type == "directory"
      && elem name [
        "target"
        ".sbt-purifier"
        ".bsp"
        ".metals"
        ".bloop"
      ]
    )
    || name == ".git"
    || name == ".idea"
    || substring 0 6 name == "result";

  files =
    dir: rel:
    let
      listing = readDir dir;
    in
    concatLists (
      map (
        name:
        let
          type = listing.${name};
          path = "${rel}/${name}";
        in
        if outsideTheBuild name type then
          [ ]
        else if type == "directory" then
          files (dir + "/${name}") path
        else
          [ path ]
      ) (attrNames listing)
    );

  rootListing = readDir root;
  sbtFiles = filter (
    name:
    let
      type = rootListing.${name};
    in
    type != "directory"
    && stringLength name > 4
    && substring (stringLength name - 4) 4 name == ".sbt"
    && !outsideTheBuild name type
  ) (attrNames rootListing);

  projectFiles = if pathExists (root + "/project") then files (root + "/project") "project" else [ ];
in
hashString "sha256" (
  concatStringsSep "" (
    map (rel: "${rel} ${hashFile "sha256" (root + "/${rel}")}\n") (sbtFiles ++ projectFiles)
  )
)
