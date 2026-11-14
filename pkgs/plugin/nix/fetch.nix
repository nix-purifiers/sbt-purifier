# The fetcher of the sbt wrapper and the plugin's interactive mode, which
# evaluate without nixpkgs.
#
# Each download is a fixed-output derivation on Nix's built-in fetcher (the
# same derivation <nix/fetchurl.nix> makes). A fixed-output store path
# depends only on its name and hash, so it is the store path that nixpkgs'
# fetchurl, and the purifier library's fetchEntry, give the same file. The
# built-in fetcher downloads from `url` alone.
#
# `fetchurl`, when given, replaces the built-in fetcher. It is called with
# { name, url, hash }.
{
  fetchurl ? null,
}:
let
  # A store path name from a file name. Store path names allow
  # [A-Za-z0-9+._?=-], must not start with a dot, and are at most 211
  # characters; every other character becomes "_".
  storeName =
    fileName:
    let
      # builtins.split alternates the text between matches with a list per
      # match.
      replaced = builtins.concatStringsSep "" (
        map (part: if builtins.isList part then "_" else part) (
          builtins.split "[^A-Za-z0-9+._?=-]" fileName
        )
      );
      undotted = builtins.head (builtins.match "\\.*(.*)" replaced);
      nonEmpty = if undotted == "" then "file" else undotted;
    in
    builtins.substring 0 200 nonEmpty;
in
{
  # The fetch name of a download: its file name, made a valid store name.
  nameOf = artifact: storeName (builtins.baseNameOf artifact.path);

  fetch =
    artifact:
    let
      name = storeName (builtins.baseNameOf artifact.path);
    in
    if fetchurl != null then
      fetchurl {
        inherit name;
        inherit (artifact) url hash;
      }
    else
      derivation {
        inherit name;
        builder = "builtin:fetchurl";
        system = "builtin";
        inherit (artifact) url;
        urls = [ artifact.url ];
        outputHash = artifact.hash;
        outputHashAlgo = "sha256";
        outputHashMode = "flat";
        unpack = false;
        executable = false;
        preferLocalBuild = true;
        impureEnvVars = [
          "http_proxy"
          "https_proxy"
          "ftp_proxy"
          "all_proxy"
          "no_proxy"
        ];
      };
}
