# Local workflow

Local only: repository `ourapps-ae/ourapps-release` has no commits, pushes, remote changes or publishing without George's say-so. Licence wording remains deferred (`licenceStatus: "deferred"`).
Suite commands (from the package root): `node --test tests/release.test.ts`.
Review a fetched archive: `python3 -m aepm review <digest> --store STORE --policy POLICY --trusted --allow-network`.
Always run Python with `-B`; never write bytecode into the repository.
