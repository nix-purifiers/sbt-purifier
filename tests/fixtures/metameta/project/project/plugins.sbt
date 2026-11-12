// sbt-nix-backend is installed here, in the meta-most build, and only here.
// It passes itself on to the meta-build (project/) and to the build, so the
// .sbt files of both can use its keys, and it governs the resolution of
// both, project/plugins.sbt included.
//
// SBT_NIX_BACKEND_REPOSITORY names the repository it comes from: the
// sbt-nix-backend flake's dev shell sets it, and so does a Nix build of a
// lockfile made with it. Outside those environments, use the published
// plugin from GitHub Pages.
val backendRepository = sys.env.get("SBT_NIX_BACKEND_REPOSITORY")
resolvers += MavenRepository(
  "sbt-nix-backend",
  backendRepository.map(path => new File(path).toURI.toString)
    .getOrElse("https://nix-purifiers.github.io/sbt-purifier/maven")
)
val backendVersion = backendRepository.fold("0.1.4") { _ =>
  sys.env.getOrElse("SBT_NIX_BACKEND_PLUGIN_VERSION", sys.error("missing supplied plugin version"))
}
addSbtPlugin("io.github.clhodapp" % "sbt-nix-backend" % backendVersion)
