import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expand, parseTargets, renderRelease, secretName, SettingsError, validateSettings, type ReleaseSettings } from "../implementation/server/render.ts";
import { buildPipeline } from "../examples/consumer.ts";

const fixture = JSON.parse(readFileSync(new URL("fixtures/settings.json", import.meta.url), "utf8"));
const all: ReleaseSettings = fixture.all;
const webOnly: ReleaseSettings = fixture.webOnly;
const rendered = renderRelease(all, { serverDirectory: "server", extra: { minPlayers: 2, maxPlayers: 9 }, description: "A demonstration." });
const text = (path: string) => rendered.files[path];
const python = (...args: string[]) => spawnSync("python3", ["-B", ...args], { encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });

// ---- settings ----

test("targets are space-separated lower-case words in fixed order; unknown, repeated, comma-joined and upper-case names are refused", () => {
  assert.deepEqual(parseTargets("tvos web  ios"), ["ios", "web", "tvos"]);
  for (const bad of ["linux", "web,ios", "WEB", "web web"]) assert.throws(() => parseTargets(bad), SettingsError, bad);
  assert.throws(() => parseTargets(""), SettingsError);
});

test("secret references map to repository secret names and a value is refused", () => {
  assert.equal(secretName("store/demo"), "STORE_DEMO");
  assert.equal(secretName("android.signing-key"), "ANDROID_SIGNING_KEY");
  for (const bad of ["has space", "1starts", "token=abc", "a".repeat(129), ""]) assert.throws(() => secretName(bad), SettingsError, bad);
});

test("invalid settings are refused with a message that names the problem", () => {
  for (const [change, word] of fixture.invalid) assert.throws(() => validateSettings({ ...all, ...change }), (error: Error) => error instanceof SettingsError && error.message.includes(word), JSON.stringify(change));
  assert.throws(() => validateSettings({ ...all, webDeploy: "store/demo" }), /different secrets/);
  assert.throws(() => validateSettings({ ...webOnly, targets: "android" }), /androidSigning/);
  assert.deepEqual(validateSettings({ ...webOnly, targets: "android web", androidSigning: "android/signing" }).targets, ["android", "web"]);
});

test("the store address loses a trailing slash and localhost may use http", () => {
  assert.equal(validateSettings(webOnly).manifestUrl, "https://ourapps.app");
  assert.equal(validateSettings({ ...webOnly, manifestUrl: "http://localhost:8787" }).manifestUrl, "http://localhost:8787");
});

test("store credentials are optional: unset or empty means no store, a value is refused", () => {
  const { storeCredentials: _unused, ...without } = all;
  assert.equal(validateSettings(without as ReleaseSettings).storeCredentials, null);
  assert.equal(validateSettings({ ...all, storeCredentials: "" }).storeCredentials, null);
  assert.equal(validateSettings(all).storeCredentials, "store/demo");
  assert.throws(() => validateSettings({ ...all, storeCredentials: "has space" }), /secret reference/);
  assert.throws(() => validateSettings({ ...all, storeCredentials: "x", androidSigning: "x" }), /different secrets/);
});

// ---- rendering ----

test("a full set of targets renders every workflow, script and the application description", () => {
  assert.deepEqual(Object.keys(rendered.files).sort(), [
    ".github/workflows/apple-testflight.yml", ".github/workflows/ci-android.yml", ".github/workflows/ci-server.yml", ".github/workflows/ci-web.yml",
    ".github/workflows/platform-packages.yml", ".github/workflows/release.yml", "release/app.json", "scripts/build-apple-testflight.sh",
    "scripts/build-windows.sh", "scripts/inspect-windows.ps1"]);
  assert.deepEqual(rendered.executable.sort(), ["scripts/build-apple-testflight.sh", "scripts/build-windows.sh"]);
  for (const [path, content] of Object.entries(rendered.files)) assert.doesNotMatch(content, /@@/, path);
});

test("secrets appear only as the names of repository secrets, never as values", () => {
  const release = text(".github/workflows/release.yml");
  for (const name of ["STORE_DEMO_APP_ID", "STORE_DEMO_BUILD_TOKEN", "ANDROID_SIGNING_KEYSTORE_BASE64", "ANDROID_SIGNING_KEYSTORE_PASSWORD", "ANDROID_SIGNING_KEY_ALIAS", "ANDROID_SIGNING_KEY_PASSWORD", "CLOUDFLARE_WEB_API_TOKEN", "CLOUDFLARE_WEB_ACCOUNT_ID"]) assert.match(release, new RegExp(`\\$\\{\\{ secrets\\.${name} \\}\\}`), name);
  for (const content of Object.values(rendered.files)) {
    const without = content.replace(/\$\{\{ secrets\.[A-Z0-9_]+ \}\}/g, "");
    assert.doesNotMatch(without, /ghp_[A-Za-z0-9]{20}|BEGIN [A-Z ]*PRIVATE KEY|Bearer [A-Za-z0-9]{20,}/);
  }
  assert.match(release, /DEMO_KEYSTORE: \$\{\{ runner\.temp \}\}\/release\.jks/);
});

// The jobs of a workflow as text, to check the order and conditions of the switch reads.
function steps(workflow: string, job: string): string[] {
  const body = workflow.split(new RegExp(`\\n  ${job}:\\n`))[1].split(/\n  [a-z-]+:\n|\n  # /)[0];
  return body.split("\n      - ").slice(1).map((step) => "      - " + step);
}

test("each release job reads its target switch first and gates every later step on it", () => {
  const release = text(".github/workflows/release.yml");
  for (const [job, target] of [["android", "android"], ["web", "web"], ["ios", "ios"], ["testflight", "ios"]]) {
    const list = steps(release, job);
    assert.match(list[0], /uses: actions\/checkout@v4/, job);
    assert.match(list[1], new RegExp(`release_targets\\.py check ${target}`), job);
    assert.match(list[1], /id: target/, job);
    for (const step of list.slice(2)) {
      if (step.includes("name: Remove signing")) continue;
      assert.match(step, /if: steps\.(target|late)\.outputs\.on == 'true'/, step.split("\n")[0]);
    }
  }
});

test("the web job reads the switch again just before deploying", () => {
  const list = steps(text(".github/workflows/release.yml"), "web");
  const late = list.findIndex((step) => step.includes("id: late"));
  const deploy = list.findIndex((step) => step.includes("id: deploy"));
  assert.equal(deploy, late + 1);
  assert.match(list[late], /release_targets\.py check web/);
  assert.match(list[deploy], /if: steps\.late\.outputs\.on == 'true'/);
  assert.match(list[deploy + 1], /if: steps\.late\.outputs\.on == 'true'/);
  assert.match(list[deploy], /CLOUDFLARE_API_TOKEN: \$\{\{ secrets\.CLOUDFLARE_WEB_API_TOKEN \}\}/);
});

test("the publish job waits for every built target and runs only when something is switched on", () => {
  const release = text(".github/workflows/release.yml");
  assert.match(release, /needs: \[version, android, web, ios, testflight\]/);
  assert.match(release, /needs\.android\.result == 'success' \|\| needs\.version\.outputs\.android != 'true'/);
  assert.match(release, /&& \(needs\.version\.outputs\.android == 'true' \|\| needs\.version\.outputs\.web == 'true' \|\| needs\.version\.outputs\.ios == 'true'\)/);
  assert.match(release, /python3 components\/ourapps-release\/server\/scripts\/publish_release\.py --app release\/app\.json/);
  assert.match(release, /PIPELINE_TARGETS: "ios android web macos windows tvos"/);
});

test("manual entry points require their target before any other work", () => {
  for (const [path, job] of [[".github/workflows/apple-testflight.yml", "export"], [".github/workflows/platform-packages.yml", "apple"], [".github/workflows/platform-packages.yml", "windows"]]) {
    const list = steps(text(path), job);
    const guard = list.findIndex((step) => step.includes("release_targets.py require"));
    assert.ok(guard >= 1, path);
    assert.ok(list.slice(0, guard).every((step) => step.includes("actions/checkout")), path);
  }
});

test("the Apple script requires its target before creating output, signing material or calling the store", () => {
  const script = text("scripts/build-apple-testflight.sh");
  const guard = script.indexOf("release_targets.py\" require \"$PLATFORM\"");
  assert.ok(guard > 0);
  assert.ok(guard < script.indexOf('mkdir "$4"'));
  assert.ok(guard < script.indexOf("curl "));
  assert.ok(guard < script.indexOf("security create-keychain"));
  assert.match(script, /DEMO_TEAM=\$\(jq/);
  assert.match(script, /check-export "\$PLATFORM" .* "app\.example\.demo"/);
});

test("subsets of targets render only what they need", () => {
  const web = renderRelease(webOnly);
  assert.deepEqual(Object.keys(web.files).sort(), [".github/workflows/ci-web.yml", ".github/workflows/release.yml", "release/app.json"]);
  const release = web.files[".github/workflows/release.yml"];
  assert.doesNotMatch(release, /^  (android|ios|testflight):$/m);
  assert.doesNotMatch(release, /keystore|xcodegen|gradle/i);
  assert.match(release, /needs: \[version, web\]/);
  assert.match(release, /&& \(needs\.version\.outputs\.web == 'true'\)/);
  assert.doesNotMatch(release, /CLOUDFLARE_API_TOKEN: \$\{\{/);
  const desktopOnly = renderRelease({ ...webOnly, targets: "windows" });
  assert.deepEqual(Object.keys(desktopOnly.files).sort(), [".github/workflows/platform-packages.yml", "release/app.json", "scripts/build-windows.sh", "scripts/inspect-windows.ps1"]);
  assert.ok(desktopOnly.notes.some((note) => note.includes("release.yml is not rendered")));
  const mac = renderRelease({ ...webOnly, targets: "macos", bundleIdentifier: "app.example.demo" });
  assert.match(mac.files[".github/workflows/apple-testflight.yml"], /options: \[macos\]/);
  assert.match(mac.files[".github/workflows/platform-packages.yml"], /options: \[macos\]/);
  assert.doesNotMatch(mac.files[".github/workflows/platform-packages.yml"], /windows:/);
});

// ---- without store credentials ----

const { storeCredentials: _removed, ...noStoreSettings } = all;
const bare = renderRelease(noStoreSettings as ReleaseSettings, { serverDirectory: "server" });
const bareText = (path: string) => bare.files[path];

test("without store credentials no rendered file reads a store secret, calls the store or uploads through it", () => {
  assert.deepEqual(Object.keys(bare.files).sort(), [
    ".github/workflows/ci-android.yml", ".github/workflows/ci-server.yml", ".github/workflows/ci-web.yml",
    ".github/workflows/platform-packages.yml", ".github/workflows/release.yml", "release/app.json", "scripts/build-windows.sh", "scripts/inspect-windows.ps1"]);
  assert.deepEqual(bare.executable, ["scripts/build-windows.sh"]);
  for (const [path, content] of Object.entries(bare.files)) {
    assert.doesNotMatch(content, /@@/, path);
    assert.doesNotMatch(content, /STORE_APP_ID|STORE_BUILD_TOKEN|STORE_API|STORE_DEMO|api\/build|testflight/i, path);
    assert.doesNotMatch(content, /STORE_[A-Z_]*\$\{\{ secrets/, path);
  }
  assert.ok(bare.notes.some((note) => note.includes("apple-testflight.yml") && note.includes("not rendered")));
  assert.ok(bare.notes.some((note) => note.includes("No iOS job")));
});

test("without store credentials the release builds the other targets, reads switches offline and publishes to GitHub", () => {
  const release = bareText(".github/workflows/release.yml");
  assert.match(release, /STORE_MODE: "none"/);
  assert.match(release, /PIPELINE_TARGETS: "android web macos windows tvos"/);
  assert.match(release, /release_targets\.py read/);
  assert.match(release, /release_targets\.py check android/);
  assert.match(release, /release_targets\.py check web/);
  assert.match(release, /needs: \[version, android, web\]/);
  assert.doesNotMatch(release, /^  (ios|testflight):$/m);
  assert.doesNotMatch(release, /needs\.version\.outputs\.ios|xcodegen|signing\.json/);
  assert.match(release, /&& \(needs\.version\.outputs\.android == 'true' \|\| needs\.version\.outputs\.web == 'true'\)/);
  assert.match(release, /publish_release\.py --app release\/app\.json/);
  assert.match(release, /# reference only: Android signing android\/signing, web hosting cloudflare\/web\./);
  assert.match(release, /DEMO_KEYSTORE: /);
  const packages = bareText(".github/workflows/platform-packages.yml");
  assert.match(packages, /STORE_MODE: "none"/);
  assert.match(packages, /release_targets\.py require "\$PLATFORM"/);
  assert.match(packages, /options: \[macos, tvos, windows\]/);
});

test("without store credentials a web-only pipeline needs no secret at all", () => {
  const { storeCredentials: _s, ...settings } = webOnly;
  const web = renderRelease(settings as ReleaseSettings);
  const release = web.files[".github/workflows/release.yml"];
  assert.doesNotMatch(release, /secrets\./);
  assert.match(release, /# reference only: none\./);
  assert.match(release, /PIPELINE_TARGETS: "web"/);
  assert.match(release, /needs: \[version, web\]/);
});

test("an iOS-only pipeline without store credentials renders no release workflow and says why", () => {
  const { storeCredentials: _s, ...settings } = webOnly;
  const ios = renderRelease({ ...settings, targets: "ios", bundleIdentifier: "app.example.demo" } as ReleaseSettings);
  assert.deepEqual(Object.keys(ios.files).sort(), ["release/app.json"]);
  assert.ok(ios.notes.some((note) => note.includes("release.yml is not rendered")));
  assert.ok(ios.notes.some((note) => note.includes("No iOS job")));
});

test("rendered store-less workflows parse as YAML with the expected jobs", () => {
  const directory = mkdtempSync(join(tmpdir(), "release-"));
  buildPipeline(directory, noStoreSettings as ReleaseSettings, { serverDirectory: "server" });
  const result = python(new URL("check_rendered.py", import.meta.url).pathname, directory);
  assert.equal(result.status, 0, result.stderr);
  const report = JSON.parse(result.stdout);
  if (report.yaml) {
    assert.deepEqual(report.workflows["release.yml"], { version: [], android: ["version"], web: ["version"], publish: ["version", "android", "web"] });
    assert.ok(!("apple-testflight.yml" in report.workflows));
  }
});

test("directories, versions, runners and the signing prefix are options, checked as plain values", () => {
  const custom = renderRelease(all, { webDirectory: "client/web", androidDirectory: "client/android", nodeVersion: "24", javaVersion: "21", macRunner: "macos-15", signingPrefix: "RELEASE" });
  const release = custom.files[".github/workflows/release.yml"];
  assert.match(release, /working-directory: client\/web/);
  assert.match(release, /node-version: "24"/);
  assert.match(release, /java-version: "21"/);
  assert.match(release, /runs-on: macos-15/);
  assert.match(release, /RELEASE_KEYSTORE_PASSWORD/);
  for (const bad of [{ webDirectory: "../web" }, { webDirectory: "/abs" }, { webDirectory: "a b" }, { nodeVersion: "22; rm" }, { macRunner: "x y" }, { signingPrefix: "lower" }, { productName: "<x>" }, { description: "" }]) {
    assert.throws(() => renderRelease(all, bad as never), SettingsError, JSON.stringify(bad));
  }
});

test("application description holds the settings and the extra manifest fields", () => {
  const app = JSON.parse(text("release/app.json"));
  assert.deepEqual(app.targets, ["ios", "android", "web", "macos", "windows", "tvos"]);
  assert.equal(app.id, "demo");
  assert.equal(app.bundleId, "app.example.demo");
  assert.deepEqual(app.extra, { minPlayers: 2, maxPlayers: 9 });
  assert.equal(app.description, "A demonstration.");
});

test("template language: blocks, inline conditions and unknown names", () => {
  const flags = new Set(["web"]);
  assert.equal(expand("a\n@@if web@@\nb\n@@end@@\n@@if ios@@\nc\n@@end@@\nd @@if ios@@x@@end@@@@if web@@y@@end@@ @@name@@", flags, { name: "N" }), "a\nb\nd y N");
  assert.throws(() => expand("@@missing@@", flags, {}), SettingsError);
  assert.throws(() => expand("@@if nonsense@@\n@@end@@", flags, {}), SettingsError);
  assert.throws(() => expand("@@if web@@\nx", flags, {}), SettingsError);
  assert.throws(() => expand("x\n@@end@@", flags, {}), SettingsError);
});

// ---- the rendered files against other tools ----

test("rendered workflows parse as YAML and every job's needs names a job", () => {
  const directory = mkdtempSync(join(tmpdir(), "release-"));
  const written = buildPipeline(directory, all, { serverDirectory: "server" });
  assert.equal(written.written.length, 10);
  assert.equal(statSync(join(directory, "scripts/build-windows.sh")).mode & 0o111, 0o111);
  assert.ok(written.notes.length >= 3);
  const result = python(new URL("check_rendered.py", import.meta.url).pathname, directory);
  assert.equal(result.status, 0, result.stderr);
  const report = JSON.parse(result.stdout);
  if (report.yaml) {
    assert.deepEqual(report.workflows["release.yml"], { version: [], android: ["version"], web: ["version"], ios: ["version"], testflight: ["version", "ios"], publish: ["version", "android", "web", "ios", "testflight"] });
    assert.deepEqual(Object.keys(report.workflows).sort(), ["apple-testflight.yml", "ci-android.yml", "ci-server.yml", "ci-web.yml", "platform-packages.yml", "release.yml"]);
  }
  for (const script of ["scripts/build-windows.sh", "scripts/build-apple-testflight.sh"]) {
    const syntax = spawnSync("bash", ["-n", join(directory, script)], { encoding: "utf8" });
    assert.equal(syntax.status, 0, `${script}: ${syntax.stderr}`);
  }
});

test("the rendered application description builds a valid manifest with the shipped script", () => {
  const directory = mkdtempSync(join(tmpdir(), "release-"));
  buildPipeline(directory, all, { extra: { minPlayers: 2 } });
  const inputs = join(directory, "inputs.json");
  const out = join(directory, "manifest.json");
  const base = new URL("../implementation/server/scripts/app_manifest.py", import.meta.url).pathname;
  writeFileSync(inputs, JSON.stringify({ version: "1.0.0", iconUrl: "https://api.github.com/a/icon.png", apkUrl: "https://api.github.com/a/app.apk", webUrl: "https://demo.example/", testflightUrl: "https://testflight.apple.com/join/X", screenshots: [], screenshotsByPlatform: {} }));
  assert.equal(python(base, "build", join(directory, "release/app.json"), inputs, out).status, 0);
  assert.equal(python(base, "validate", out).status, 0);
  const manifest = JSON.parse(readFileSync(out, "utf8"));
  assert.equal(manifest.minPlayers, 2);
  assert.equal(manifest.install.ios.bundleId, "app.example.demo");
});

// ---- the Python parts ----

test("release targets, manifest, packages, artifacts and publishing pass their Python checks", { timeout: 170000 }, () => {
  const result = python(new URL("test_release.py", import.meta.url).pathname);
  assert.equal(result.status, 0, result.stderr.split("\n").slice(-30).join("\n"));
  assert.match(result.stderr, /Ran \d+ tests/);
  assert.match(result.stderr, /\nOK\n?$/);
});
