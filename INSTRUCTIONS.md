# Ourapps release instructions

## For the agent
Select `ourapps-ae/ourapps-release` for every recipe that has a client target; set `targets` from the targets answer (map the recipe's `ios` to `ios`, `macos` to `macos`, and so on). Set `storeCredentials` only when the developer has a store account (the recipe's updates question supplies it); leave it `none` otherwise and the pipeline skips every store step. Give each reference setting as a reference name (for example `<application>-store`), never a value, and tell the developer which repository secrets that creates. Set `repository` to `https://github.com/<owner>/<name>` and `manifestUrl` to `https://api.github.com/repos/<owner>/<name>/releases/latest` when the repository is known. Use the same signing prefix as android-host and apple-host.

- Select package `ourapps-ae/ourapps-release` version `0.5.0`.
- Settings to configure:
  - `targets` (string, label; required)
  - `applicationId` (string, slug; required)
  - `applicationName` (string, label; required)
  - `storeAddress` (string, address; optional; default `https://ourapps.app`)
  - `manifestUrl` (string, address; optional)
  - `repository` (string, address; optional)
  - `storeCredentials` (string, slug; optional; default `none`; a reference name, never a value)
  - `bundleIdentifier` (string, qualified; optional)
  - `androidSigning` (string, slug; optional; default `none`; a reference name, never a value)
  - `webDeploy` (string, slug; optional; default `none`; a reference name, never a value)
  - `hostingAccount` (string, slug; optional; default `none`; a reference name, never a value)
- Connect the provided `release-pipeline` (ae/release-pipeline 1.0.0) to the consumer that needs it.
- References are given by name only, never by value.

## For the developer
Create the repository secrets named by the references (`<NAME>_APP_ID` and `<NAME>_BUILD_TOKEN` from the store, only when `storeCredentials` is set; for Android a keystore as base64 plus its password, alias and key password; for Cloudflare web hosting an API token and account id, on an account separate from the store's). Provide `CHANGELOG.md` with a section for each released version, the icon and screenshots, and the Apple and desktop build scripts named in `result.notes`. Run the render step, commit the generated files, then push a `v*` tag. To check, run the commands below.

## How to check
From the component directory:

- `node --test tests/release.test.ts` (release, required-local; tools: node 26.5, python3 3.12)
- `python3 -m aepm review <digest> --store STORE --policy POLICY --trusted --allow-network` checks an archive fetched with `python3 -m aepm fetch ourapps-ae/ourapps-release@VERSION --store STORE --from https://ourapps.app` and runs its suites.
