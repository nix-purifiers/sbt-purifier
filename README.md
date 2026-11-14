# sbt-purifier

sbt-purifier keeps sbt's development workflow on locked,
Nix-resolved dependencies. Use the normal build and test commands in your
working tree; building the project itself in the Nix sandbox is an
optional next step.

- **A lockfile.** `sbt-purifier lock` writes `sbt-purifier.lock.json` at
  the build root, with every file the build downloads (by URL, mirrors and
  SHA-256), the sbt launcher, the JVM options, and the projects to build.
- **Interactive sbt on the locked downloads.** With a lockfile present,
  `compile`, `run` and `test` stay incremental, but resolution reads only
  the locked files, through Nix, offline. That holds at every level of the
  build, including the plugins in `project/plugins.sbt`. The optional
  wrapper also boots sbt itself from locked downloads.
- **Sandboxed builds.** `buildSbtProject`, which lives in the
  `sbt-purifier` scope that the flake's `overlays.default` adds to the
  package set, turns a build and its lockfile into derivations, one
  package per project.
- **Builds that depend on builds.** `nixFlakeRepositories` names another
  flake's repository output; resolution reads it first, and the lockfile
  pins its revision.
- **Publishing what Nix built.** `sbt-purifier publish` runs sbt's
  `publish` with the artifacts the Nix sandbox built in place of locally
  packaged ones.

## Quickstart

From the project root:

```zsh
nix shell 'github:nix-purifiers/sbt-purifier#sbt'
sbt nixLock
sbt compile
sbt test
```

The wrapper supplies the plugin through Nix. Commit
`sbt-purifier.lock.json` after locking. To add the plugin to an existing
sbt installation instead, see [From the Maven repository](#from-the-maven-repository).

Install the wrapper through your usual Nix configuration.

## Development loop

With the plugin installed and a lockfile committed, keep using sbt:

```zsh
sbt compile test
sbt
```

Compilation, tests and packaging remain sbt tasks in the working tree.
The plugin has Nix realize the locked dependencies and points sbt's
resolution at them. You do not need a project Nix build definition for
this development loop.

To add or upgrade a dependency, edit the build and lock again. The purifier
contract has a command that needs an unlocked download stop with a message
naming the lock command. sbt-purifier stops that way while the lockfile
matches the build definition. After an edit to the build definition, sbt
warns that the lockfile was made from another build definition and
resolves the usual way, from the network, so the edited build loads and
`sbt nixLock` can run in it:

```zsh
sbt nixLock         # relocks when the build definition changed
# resolves dependencies afresh and moves the flake repositories
sbt nixLockUpdate
```

`nixLock` keeps a lockfile that is up to date as it is, and a relock of an
unchanged build reproduces it byte for byte. The purifier contract has
`update <package>...` update only the named packages. sbt resolves the
whole build at once, so `sbt nixLockUpdate` takes no package names: it
re-resolves every dependency, version ranges and dynamic versions
included, and `sbt-purifier.lock.json` is the one lockfile that changes.
`nixLock` keeps the revision the lockfile records for each flake repository
(see "Supported language repositories"), and `nixLockUpdate` locks each one at
the newest revision of its reference. `sbt-purifier publish` runs
`publish` with the artifacts Nix built; `publishTo`, credentials and the
publishing style work as they do for `publish`, and `+nixPublish` in sbt
publishes every Scala version.

The lock run downloads every dependency afresh. When Maven Central limits
the rate of a large run, `SBT_NIX_BACKEND_MAVEN_CENTRAL_MIRROR` names a
mirror that serves the same files, and the lock run downloads the files
of Maven Central from it; the lockfile still records their Maven Central
URLs, with Google's mirror of Maven Central among their mirrors:

```zsh
SBT_NIX_BACKEND_MAVEN_CENTRAL_MIRROR=https://maven-central.storage-download.googleapis.com/maven2/ \
  sbt nixLock
```

### Updating sbt-purifier

Locks created through the wrapper pin sbt-purifier's source revision and hash,
selecting the wrapper and its plugin together. Updating the installed wrapper or
running `sbt-purifier update` preserves this pin. To change it explicitly:

```sh
sbt-purifier self-update --to installed
```

Use `--to github:nix-purifiers/sbt-purifier/REV` to select a published revision.
The selected implementation relocks a staged copy before replacing the project
lock. `sbt-purifier implementation` prints its identity. Existing unpinned locks
warn until explicitly migrated. Use an implementation-aware wrapper for pinned
projects: a separately loaded plugin cannot switch its own implementation and
rejects a mismatch. Projects need no flake.

Use this source's exported wrappers for portable pins. Package and scope
changes that alter their recipes are rejected rather than claiming the default
source reproduces custom wrapper code. This guard does not restrict arbitrary
customizations of the separate project builder.

Pinned projects run in sbt's foreground mode so an existing daemon cannot keep
an older plugin after a pin changes. `--client`, `--jvm-client` and
`SBT_NATIVE_CLIENT=true` are rejected while the pin is active. Ordinary commands
such as `sbt test` work unchanged; reusing a long-lived sbt server is not yet
supported for pinned projects.

### Resolving the plugin through Nix

The optional sbt wrapper loads the plugin and boots sbt from Nix-resolved
inputs too. It provides pure resolution of the plugin itself and keeps
the ordinary sbt command line:

```zsh
nix shell 'github:nix-purifiers/sbt-purifier#sbt'
sbt compile test
```

Local commands use your existing Java runtime through `JAVA_HOME`, `JAVACMD`,
`PATH`, or an explicit `--java-home` argument. Capture records the actual
Java major. The upstream sbt and project requirements still apply.
The `sbt-purifier lock` and `sbt-purifier update` commands wrap
`sbt nixLock` and `sbt nixLockUpdate`; `sbt nixRelock` forces a relock
while keeping the pinned flake repositories.

### From the Maven repository

Each release is in a Maven repository on GitHub Pages, which sbt reads
without credentials. Install it in the meta-most build, with the version
of the newest [release](https://github.com/nix-purifiers/sbt-purifier/releases):

```scala
resolvers += "sbt-nix-backend" at
  "https://nix-purifiers.github.io/sbt-purifier/maven"
addSbtPlugin("io.github.clhodapp" % "sbt-nix-backend" % "<version>")
```

sbt downloads the plugin like any other plugin, so the lockfile records it
with everything else, and a Nix build of the lockfile needs neither the
wrapper nor the flake's plugin repository.

When Maven resolves the plugin outside the Nix store, it warns that the
plugin is being resolved impurely and recommends the wrapper for pure
resolution. A plugin supplied by Nix, including a cache link into the
store, loads without that warning.

### Without the wrapper

A build whose `.sbt` files use the plugin's keys needs the plugin in its
own definition. Install it once, in `project/project/plugins.sbt`:

```scala
val backendRepository = sys.env.get("SBT_NIX_BACKEND_REPOSITORY")
resolvers += MavenRepository(
  "sbt-nix-backend",
  backendRepository.map(path => new File(path).toURI.toString)
    .getOrElse("https://nix-purifiers.github.io/sbt-purifier/maven")
)
addSbtPlugin(
  "io.github.clhodapp" % "sbt-nix-backend" %
    backendRepository.fold("0.1.4") { _ =>
      sys.env.getOrElse("SBT_NIX_BACKEND_PLUGIN_VERSION", sys.error("missing supplied plugin version"))
    }
)
```

`nix shell 'github:nix-purifiers/sbt-purifier#sbt'` provides the sbt
wrapper, which sets `SBT_NIX_BACKEND_REPOSITORY` to the plugin repository
when it launches sbt, and supplies the matching version in
`SBT_NIX_BACKEND_PLUGIN_VERSION`. The example uses that source-built version and the
published version, `0.1.4`, from GitHub Pages otherwise. In a
meta-build, the plugin adds itself to the build the meta-build defines,
so `project/` and the build both have it.

### Keys

| Key | What it does |
|---|---|
| `nixLock` | Writes the lockfile when the build definition changed (`sbt-purifier lock`). |
| `nixRelock` | Writes the lockfile, keeping the revisions of the flake repositories. |
| `nixLockUpdate` | Writes the lockfile and locks every flake repository afresh (`sbt-purifier update`). |
| `nixPublish` | A command: `publish`, with the artifacts Nix built (`sbt-purifier publish`). |
| `nixPackagedArtifacts` | `publish / packagedArtifacts` for the current Scala version, built by Nix. |
| `nixRuntimeRepository` | The project's repository output, built by Nix. |
| `nixNetworkDependencies` | URLs a plugin downloads outside coursier. |
| `nixFlakeRepositories` | Flake references to other builds' repositories. |
| `nixFlakeModule(name)` | The module a flake repository publishes. |
| `nixNetworkFile(url)` | Where a declared URL is in the coursier cache. |
| `nixPackaged` | Whether the lockfile builds the project (default: `!(publish / skip)`). |
| `nixSbtJavaOptions` | JVM options of the sbt that the lock run and Nix builds run. |
| `nixNixpkgs` | The nixpkgs `nixPackagedArtifacts` builds with. |
| `nixBackend` | Whether interactive resolution reads the lockfile. |

The installed wrapper warns when `sbt-purifier.lock.json` is absent and
runs sbt with ordinary, impure dependency resolution. Create the lock with
`sbt nixLock`. A missing lock does not prevent using sbt in other projects.

## Building the project with Nix

Once the native development workflow is working, you can also build and
test the project in the Nix sandbox. For this step, keep a flake or
another Nix build definition in the project's repository, or maintain an
external packaging repository that points at its sources and lockfiles.
The builder consumes the same lockfiles as the development workflow.

A project whose authors want a flake can package the build in one. The
flake applies the overlay to its package set and calls the builder, and it
can live in the project's repository or in another one:

```nix
{
  inputs.sbt-purifier.url =
    "github:nix-purifiers/sbt-purifier";
  inputs.nixpkgs.follows = "sbt-purifier/nixpkgs";

  outputs =
    { nixpkgs, sbt-purifier, ... }:
    let
      system = "x86_64-linux";
      pin = (builtins.fromJSON
        (builtins.readFile ./sbt-purifier.lock.json)).implementation;
      selected =
        (sbt-purifier.lib.purifier.implementationSource.resolve
          pin.source).flake;
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ selected.overlays.default ];
      };
      build = pkgs.sbt-purifier.buildSbtProject {
        src = ./.;
      };
    in
    {
      packages.${system}.core = build.packages.core;
      checks.${system}.tests = build.tests;
      # Other builds reference these (see "Supported
      # language repositories").
      sbtRepositories.${system} = build.sbtRepositories;
    };
}
```

### The builder

`overlays.default` adds one attribute to the package set, the scope
`pkgs.sbt-purifier`. The scope holds `buildSbtProject`, the plugin,
`plugin-repository` and the `sbt` wrapper. A function that `callPackage`
calls takes the scope by the name `sbt-purifier`.

`buildSbtProject` is itself a package for `callPackage`. It takes the
packages it needs by name, such as `javaPackages` and `fetchurl`.
`sbt-purifier.buildSbtProject.override` replaces any of them, and
`sbt-purifier.overrideScope` replaces what the scope shares.
`buildSbtProject { src, lockFile ? src + "/sbt-purifier.lock.json", fetchers ? { }, ... }`
returns:

- `packages.<project>`: the project's published artifacts, in the layout
  it publishes in. Its `repository` output holds them with their runtime
  dependencies, as a repository that resolves offline.
- `tests`: every package built with its tests run.
- `release`: the published artifacts of every package, as one repository.
- `fetches`: the lock's download entries, in the shape of the purifier
  library.
- `sbtRepositories`: each package's `repository` output by project.

`fetchers` goes to the purifier library's `tree`, so a `fetchurl` with
credentials or a company mirror replaces the default one. The other
arguments are `jdk` (the default is nixpkgs' headless JDK of the Java
major the lock records), `doCheck` (runs `test` in every package build),
`srcFilter`, `pluginRepository` and `flakeRepositories`;
`pkgs/build-sbt-project/default.nix` documents them.
Path sources are filtered during evaluation. A source produced by a
derivation remains a build input, so evaluating the builder does not build
it. Prepare that source in its producing derivation; `srcFilter` applies
only to sources that can be read during evaluation.

`lib.sbt-purifier.readLock { src, lockFile ? ... }` reads the lockfile
into the purifier library's lock in memory, which other tools read.

A build can also be kept external, for a repository that should not
hold the lockfile. The lockfile then lies outside the sbt project: the
calling flake passes it as `lockFile`, and the commands take
`--lockfile <path>` or `SBT_PURIFIER_LOCKFILE`. A lockfile outside `src`
takes the place of any lockfile in it.

On Darwin, checked Nix builds append `-Djava.net.preferIPv4Stack=true` to
`JAVA_TOOL_OPTIONS`, preserving existing options. Forked test JVMs inherit this
setting so their result transport uses IPv4 loopback with the sandbox's local
networking permission. This applies to checked package builds and the independent
offline task check; ordinary host commands, Linux builds, and unchecked packages
keep their existing settings. IPv6-only endpoints are unavailable in these checked
JVMs. The JDK 21 runtime fixture verifies three IPv4 process pairs and external
IPv4 permission denials; unavailable IPv6 is recorded separately, never as a
successful network denial. Older JDK builds do not require that modern probe.

### What a package holds

The lockfile names every artifact of every package, so Nix knows them
before it builds them. `packages.<project>.artifacts.<variant>.<key>` is
an artifact's file; the key is `<name>[-<classifier>].<extension>`, so the
sources jar of `core` built for Scala 3 is
`artifacts.scala3."core-sources.jar"`. `packages.<project>.variants` has
each variant's name, Scala version, artifacts and `descriptorFile`, its
POM or `ivy.xml`. A project has one variant for each Scala version it is
built for, named for the cross version its module is published with:

| Published as | Variant |
| --- | --- |
| `core_3` | `scala3` |
| `core_2.13` | `scala2_13` |
| `core_2.13.16` (`CrossVersion.full`) | `scala2_13_16` |
| `core_sjs1_3` (Scala.js) | `sjs1_scala3` |
| `core` (`crossPaths := false`) | `default` |
| an sbt 0.13 plugin | `scala2_10_sbt0_13` |
| an sbt 1 plugin, Maven or Ivy style | `scala2_12_sbt1_0` |
| an sbt 2 plugin (`_sbt2_3`) | `scala3_sbt2` |

The lock fails when two Scala versions of a project would have the same
variant, such as 2.13.15 and 2.13.16 with a binary cross version. The
package links each artifact at `nix-support/artifacts/<variant>/<key>`,
and the module descriptor at `nix-support/artifacts/<variant>/descriptor`.
Neither these names nor the lockfile hold the version of the project.

### One-off builds without a project Nix file

For an ad hoc build, the builder can also be evaluated directly from the
project root. Replace the example package name with one from your project:

```zsh
nix build --impure --expr 'let
  bootstrap = builtins.getFlake "github:nix-purifiers/sbt-purifier";
  pin = (builtins.fromJSON
    (builtins.readFile ./sbt-purifier.lock.json)).implementation;
  sbt-purifier =
    (bootstrap.lib.purifier.implementationSource.resolve
      pin.source).flake;
  pkgs = import sbt-purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ sbt-purifier.overlays.default ];
  };
in
(pkgs.sbt-purifier.buildSbtProject { src = ./.; }).packages.core'
```

This uses the purifier's pinned nixpkgs, matching the lock command above.
To build with ambient `<nixpkgs>` from your Nix search path instead,
replace `import sbt-purifier.inputs.nixpkgs` with `import <nixpkgs>`.
Both forms need no Nix files in your project. The build tool and language
versions must match the lockfile; with ambient nixpkgs, lock with the
purifier from that package set or pass a compatible toolchain to the builder.

## Working offline

```zsh
sbt-purifier prefetch
```

realizes every download the lockfile lists in the Nix store, with
garbage-collector roots beside the lockfile
(`sbt-purifier.lock.json.gcroot*`). The downloads include sbt itself, the
plugins of every level of the build, compiler bridges, and the sources
and javadoc jars an editor asks for. After a prefetch, sbt and the
everyday loop work with the network cut off.

Offline sandboxed project builds also need the selected `jdk` and its
closure in the Nix store. SDK selection for those builds is described above.

In interactive mode, the wrapper boots sbt from the locked downloads, and
the plugin realizes the lockfile through Nix (one garbage-collector root,
under `target/sbt-nix-backend`) into a coursier cache of links into the
store. Every level of the build resolves from it, offline.
`SBT_NIX_BACKEND=0` turns interactive mode off.

## Editor

An editor imports the build through sbt's build server, and asks it for
the sources of every dependency (`buildTarget/dependencySources`, which
runs `updateClassifiers`). The lock run resolves what a
`dependencySources` request needs: it runs `updateClassifiers` for every
project at its default Scala version, unpublished projects included, and
`updateSbtClassifiers` for the sources of sbt and of the build's plugins.
An editor that imports the build then resolves those sources from the
lock, with the network cut off.

## Troubleshooting

Every problem with a lockfile stops the command, the sbt load or the Nix
evaluation with a message that names the command that fixes it.

| Problem | Where it shows | Fix |
| --- | --- | --- |
| No lockfile | `prefetch`, `publish`, Nix evaluation | `sbt-purifier lock` |
| The build definition changed since the lock (stale) | sbt warns and resolves the usual way; `prefetch`, `publish` and Nix evaluation stop | `sbt-purifier lock` |
| The lockfile was edited or damaged (its content hash does not match) | every command, sbt, Nix evaluation | `sbt-purifier lock` |
| The lockfile is not one sbt-purifier wrote, or has another format version | every command, sbt, Nix evaluation | `sbt-purifier lock` |
| A dependency not in the lockfile | resolution in interactive mode and in the sandbox | `sbt-purifier lock` |
| A plugin version that the plugin repository does not hold | the Nix build | the sbt-purifier revision the lock was made with, or `sbt-purifier lock` |

The lockfile's third line holds the SHA-256 of the rest of the file, and
the library checks it with string functions before it parses the JSON, so
a malformed or edited lockfile is a problem with a message, not a failed
parse. The definition hash covers the `*.sbt` files at the root and
everything under `project/`, so a version set there makes the lockfile
stale like any other edit, while a version the build reads from another
file keeps it up to date.

## Supported language repositories

### How the lock is made

The lock run copies the build to `target/sbt-nix-backend/lock-run`, and
forks sbt there with an empty coursier cache, home and boot directory. The
fork runs the editor downloads and then the command a Nix build runs
(`nixSandboxBuild`), with the network. Every file it downloads goes into
the lockfile. A sandboxed build reads only through coursier, from that
cache, so the lockfile holds what it needs without a list of what sbt
fetches: the launcher's download of sbt, the meta-builds' plugins,
compiler bridges, Scaladoc tooling and the projects' dependencies alike.

### Supported

- **Maven repositories**, resolved with coursier, at every level of the
  build.
- **Ivy repositories and Ivy settings files**, on sbt 0.13 and on sbt 1
  with `useCoursier := false`. The plugin rewrites every remote resolver of such
  a project onto a mirror of the coursier cache, where the file of
  `https://host/path` sits at `https/host/path`, including the resolvers
  of an Ivy settings file and every file it includes. Such a project gets
  an Ivy home of its own under `target/sbt-nix-backend/ivy-homes`.
- **Flake repositories**: another purified JVM project's package, named in
  `nixFlakeRepositories`. The producer exports `projections.maven` or
  `projections.ivy`; legacy `sbtRepositories.<system>` references remain supported.

  ```scala
  ThisBuild / nixFlakeRepositories += NixFlakeRepository(
    "lib", "github:owner/lib/<rev>#core"
  )
  libraryDependencies += "example" %% "core" % "1.0"
  ```

  The catalog selects the supplied module's Nix-derived version automatically.
  The lock pins the producer's Nix source, without adding its artifacts to the
  download list. `nixUpdate` refreshes the pin. Nix build definitions can also
  supply `projectDependencies = [ producer.packages.${system}.core ];`.
  Projected repositories use immutable versions and rewrite POM references
  for ordinary downstream consumers.

- **Files a plugin downloads outside coursier**, when the plugin declares
  them in `nixNetworkDependencies` and reads them where the system
  property `sbt.nix.backend.coursierCache` says
  (`tests/fixtures/multi/project/LicensePlugin.scala` is an example).
- **sbt 0.13.18, sbt 1.11 and later in the 1.x line, and sbt 2.0.** The
  plugin is cross-built for Scala 2.10, 2.12 and 3. The wrapper injects the
  variant the project's sbt loads. For sbt 0.13, use the wrapper: it supplies
  Java 8 and bootstraps sbt from the official archive, including offline
  startup from a lock.
- **Cross builds and projectMatrix.** A project in `crossScalaVersions` is
  built for each of its Scala versions; a `projectMatrix` makes a package
  per Scala version.

A project publishes into its package in the layout it publishes in
anywhere: Maven layout, or sbt's standard Ivy layout for
`publishMavenStyle := false`. A `repository` output keeps every module in
the layout it was published in, and a consumer reads it with both
resolvers:

```scala
externalResolvers := Seq(
  MavenRepository("repository", "file:///nix/store/...-repository"),
  Resolver.file(
    "repository-ivy",
    file("/nix/store/...-repository")
  )(Resolver.ivyStylePatterns)
)
```

### Refused, or needing help

- A plugin that downloads outside coursier without declaring its URLs
  fails in the sandbox; the lock run lists what it wrote under its home
  directory, to help find such files.
- A lockfile records URLs, not credentials; a dependency from an
  authenticated repository needs a `fetchers.fetchurl` that supplies them.
- A `-SNAPSHOT` dependency changes under its URL, so its locked hash stops
  matching when it does; the lock run warns about them.
- A `file:` URL is read in place by coursier and never locked.
- Building in Nix needs the build's sources to be visible to Nix: in a
  flake, tracked by git.

## Examples

[pureconfig](https://github.com/pureconfig/pureconfig) is a multi-module
sbt build cross-built for Scala 2.12, 2.13 and 3. Start from its checkout:

```zsh
git clone https://github.com/pureconfig/pureconfig
cd pureconfig
```

Add the plugin as shown in [Quickstart](#quickstart), then use sbt:

```zsh
sbt nixLock
sbt compile
sbt test
```

Commit `sbt-purifier.lock.json` with the plugin declaration. After changing
dependencies, run `sbt nixLock` again. The optional wrapper also works
with this setup. If you later want sandboxed project artifacts, add a
Nix build definition as described in
[Building the project with Nix](#building-the-project-with-nix).

## Credits

The purifier idea was inspired by nixcloud's
[cargo (libnix)](https://github.com/nixcloud/cargo).

Dependency resolution uses [Coursier](https://get-coursier.io).

## Development

Source builds use `0.0.0-dev.<sha256>` from the plugin's `build.sbt`,
`project/`, `src/`, `nix/`, `flake.lock` and `sbt-purifier.lock.json` bytes.
Nix and native sbt hash the same sorted file manifest, excluding generated
`target/` directories. The self-build lock omits publication versions, so
it can identify the resolved dependencies without recording this version
inside its own fingerprint. This version does not depend on Git tags or
the checkout path.

Release builders pass the version explicitly: overriding the Nix plugin
package's `version` also supplies that version to sbt. Ordinary native sbt
accepts `SBT_NIX_BACKEND_RELEASE_VERSION` for the same explicit release use.

`docs/development/` describes the project, its tests and its commands.
