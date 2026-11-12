# SPDX-License-Identifier: MIT
# Select an inferred JVM view, retaining the old sbtRepositories interface.
{
  locked,
  attribute,
  system,
  override ? null,
}:
let
  path = builtins.filter builtins.isString (builtins.split "[.]" attribute);
  has =
    keys: value:
    keys == [ ]
    || (
      builtins.isAttrs value
      && builtins.hasAttr (builtins.head keys) value
      && has (builtins.tail keys) value.${builtins.head keys}
    );
  get =
    keys: value: if keys == [ ] then value else get (builtins.tail keys) value.${builtins.head keys};
  # A legacy repository output inherits its package's passthru, including
  # projections. Keep an explicitly selected repository output as supplied.
  typedOverride =
    builtins.isAttrs override
    && override ? projections
    && (override.outputName or "out") != "repository";
  rawOverride = override != null && !typedOverride;
  flake = builtins.getFlake locked;
  candidate =
    if typedOverride then
      override
    else if
      has (
        [
          "packages"
          system
        ]
        ++ path
      ) flake
    then
      get (
        [
          "packages"
          system
        ]
        ++ path
      ) flake
    else if
      has (
        [
          "legacyPackages"
          system
        ]
        ++ path
      ) flake
    then
      get (
        [
          "legacyPackages"
          system
        ]
        ++ path
      ) flake
    else if has path flake then
      get path flake
    else
      null;
  projections = if builtins.isAttrs candidate then candidate.projections or { } else { };
  typed = projections ? maven || projections ? ivy;
  legacy = [
    "sbtRepositories"
    system
  ]
  ++ path;
  repository =
    if rawOverride then
      override
    else if projections ? maven then
      projections.maven
    else if projections ? ivy then
      projections.ivy
    else if has legacy flake then
      get legacy flake
    else
      throw "sbt-purifier: ${locked} has no JVM projection or sbtRepositories output for ${attribute}";
  catalog = if builtins.isAttrs repository then repository.catalog or null else null;
  view = {
    inherit repository catalog;
    dependency = if rawOverride || !typed then null else candidate;
    info = {
      path = toString repository;
    }
    // (if catalog == null then { } else { catalog = toString catalog; });
    build = [
      repository
    ]
    ++ (if builtins.isAttrs repository && repository ? metadata then [ repository.metadata ] else [ ]);
  };
in
assert
  rawOverride
  || !typed
  || catalog != null
  || throw "sbt-purifier: the JVM projection of ${attribute} needs an inferred module catalog";
view
