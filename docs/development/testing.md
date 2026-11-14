# Testing

The tests come in three layers, fastest first.

1. **Scala unit tests** (`src/test/scala`, munit) cover the JSON codec, the
   coursier path mapping, the lockfile and its hashes, the build copy, the
   linked cache, POM coordinates, and that the jar carries all of
   the `pkgs/plugin/resources.tsv` manifest. `sbt test` runs them, and so does the plugin's Nix build.
2. **Flake checks** (`tests/default.nix`) run in the sandbox under
   `nix flake check`:
   - `plugin` builds the plugin from its own lockfile, unit tests included.
   - `nix-library` runs `tests/nix-library.nix`, which applies the library
     to hand-written lockfiles with a fetcher that downloads nothing.
   - The `multi-*` checks build `tests/fixtures/multi` with the plugin
     injected. The fixture has Scala 3 and Java projects, a plugin from
     `project/plugins.sbt`, a URL that a stand-in third-party plugin
     downloads, and a test suite that runs in the build. The checks show
     that each package holds only its own artifacts, and that the app's
     repository alone resolves and runs the app.
   - The `sbt2-*` checks do the same for `tests/fixtures/sbt2`, an sbt 2
     build with the plugin's sbt 2 variant injected, and `plugin-variants`
     checks that the plugin repository holds both variants.
   - `consumer-repository` builds `tests/fixtures/consumer`, which takes the
     multi app through a flake repository, and resolves and runs it from
     the consumer's own repository.
3. **Interactive tests** (`tests/interactive.sh`) run outside the sandbox
   with sbt's network cut off. On copies of multi and sbt2, run through the
   wrapper, compile, test and run
   must work from the locked downloads at every level, the user's coursier
   cache must stay empty, `nixPackagedArtifacts` must return the Nix
   build's files, and a stale lockfile must fall back with a warning. The
   metameta fixture runs the same checks under plain sbt from the dev shell,
   with the plugin installed only in project/project/plugins.sbt: the
   meta-build and the build both use its keys, and their dependencies must
   come from the lockfile rather than the user's cache.

The lockfiles of the fixtures are test data. `tests/relock-fixtures.sh`
renews them, and the lockfile of the root build too; it needs the network.
The consumer lockfile pins a `path:` reference to a temporary copy of
multi, so the flake check passes the multi repository in its place rather
than fetching that reference.
