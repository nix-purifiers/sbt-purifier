# readLock: the sbt-purifier lockfile in the shape that every purifier
# reads its lock into (CONTRACT.md of the purifier library, "The lock in
# memory"):
#
#   {
#     purifier = "sbt-purifier";
#     entries  = [ { path, url, mirrors, hash } ];  # file entries, every
#                                                  # system
#     outputs  = { <project id> = { id, organization, name, variants }; };
#     problems = [ <message naming `sbt-purifier lock`> ];
#     lock     = <the parsed lockfile, or null when it has a problem
#                 that keeps it from being read>;
#   }
#
# It needs no nixpkgs, so the plugin's interactive mode evaluates it with
# the Nix installation alone. The plugin writes the lockfile with its
# content hash on the third line (Lock.withContentHash), so a lockfile
# that sbt-purifier did not write, or that changed since, is found with
# string functions before the JSON is parsed, and becomes a problem
# rather than a failed evaluation.
{
  src,
  lockFile ? src + "/sbt-purifier.lock.json",
}:
let
  inherit (builtins)
    elemAt
    filter
    hashString
    isString
    length
    listToAttrs
    match
    pathExists
    readFile
    split
    stringLength
    substring
    ;

  fix = "run `sbt-purifier lock`";
  where = toString lockFile;
  formatVersion = import ./lock-version.nix;

  text = if pathExists lockFile then readFile lockFile else null;

  # The first three lines: `{`, the version line and the content hash
  # line. The header is short; matching the whole text with one regular
  # expression could overflow the stack of Nix's regex engine.
  lines = filter isString (split "\n" (substring 0 160 text));
  line = n: if length lines > n then elemAt lines n else null;
  version = match "  \"version\": ([0-9]+)," (line 1);
  contentHash = match "  \"contentHash\": \"([0-9a-f]{64})\"," (line 2);
  rest = substring (
    stringLength (line 0) + stringLength (line 1) + stringLength (line 2) + 3
  ) (stringLength text) text;

  contentProblem =
    if text == null then
      "${where} does not exist; ${fix}"
    else if line 0 != "{" || version == null then
      "${where} is not a lockfile that sbt-purifier wrote; ${fix}"
    else if elemAt version 0 != toString formatVersion then
      "${where} has lock format version ${elemAt version 0}, and this sbt-purifier reads version ${toString formatVersion}; ${fix}"
    else if contentHash == null then
      "${where} is not a lockfile that sbt-purifier wrote; ${fix}"
    else if hashString "sha256" "{\n${line 1}\n${rest}" != elemAt contentHash 0 then
      "${where} does not match its content hash: it was edited, or it is damaged; ${fix}"
    else
      null;

  lock = if contentProblem == null then builtins.fromJSON text else null;

  # A source that is the output of a derivation (a derivation, or a string
  # whose context names derivation outputs) would have to be built to be
  # read, which evaluation does not do; its staleness is left to the
  # plugin. A path, a flake input, or a store path of a source is read. So
  # is no source at all for a lock read with `src = null`, as the sbt
  # wrapper and the plugin's interactive mode read it for its downloads
  # alone.
  srcPath = if builtins.isAttrs src then src.outPath else src;
  readable = import ./source-readable.nix src;
  staleProblem =
    if lock == null || !readable then
      null
    else if import ./definition-hash.nix srcPath != lock.definitionHash then
      "${where} is stale: the build definition (the *.sbt files and project/) changed since it was written; ${fix}"
    else
      null;
in
{
  purifier = "sbt-purifier";
  inherit lock;
  problems = filter (p: p != null) [
    contentProblem
    staleProblem
  ];
  entries = if lock == null then [ ] else lock.artifacts;
  outputs =
    if lock == null then
      { }
    else
      listToAttrs (
        map (p: {
          name = p.id;
          value = p;
        }) lock.projects
      );
}
