import { chmodSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

// Build-time rendering of the release pipeline: GitHub Actions workflows, build scripts and the application
// description the publish step reads. Secrets appear only as the names of repository secrets.

export const TARGETS = ["ios", "android", "web", "macos", "windows", "tvos"] as const;
export type Target = (typeof TARGETS)[number];

export interface ReleaseSettings {
  // Any of ios, android, web, macos, windows, tvos, lower case, separated by spaces.
  targets: string;
  applicationId: string;
  applicationName: string;
  // The store's address (default https://ourapps.app). Its /api/build/<id>/... routes carry the target switches, signing
  // answers and uploads.
  storeAddress?: string;
  // Where the store reads my-app.json: the repository's latest release, https://api.github.com/repos/<owner>/<name>/releases/latest.
  manifestUrl?: string;
  // The application's repository, https://github.com/<owner>/<name>.
  repository?: string;
  // Reference name for the store credentials: repository secrets <NAME>_APP_ID and <NAME>_BUILD_TOKEN.
  // Optional ("none" or empty is unset). Without it the pipeline makes no store call: every target the pipeline builds is on,
  // the iPhone build and TestFlight upload (which sign and upload through the store) and the Apple TestFlight workflow are
  // not rendered, and the release still publishes to GitHub.
  storeCredentials?: string;
  // Apple bundle identifier; required for ios, macos and tvos.
  bundleIdentifier?: string;
  // Secret reference for Android signing: <NAME>_KEYSTORE_BASE64, _KEYSTORE_PASSWORD, _KEY_ALIAS, _KEY_PASSWORD. Required for android.
  androidSigning?: string;
  // Reference name for hosting the web build on Cloudflare: <NAME>_API_TOKEN and <NAME>_ACCOUNT_ID. Without it the web build is bundled as web.zip.
  webDeploy?: string;
  // Reference name for the Cloudflare account the server deploys to: <NAME>_API_TOKEN and <NAME>_ACCOUNT_ID.
  hostingAccount?: string;
}

export interface ReleaseOptions {
  // Where the shipped scripts are installed in the application repository.
  scriptsDirectory?: string;
  webDirectory?: string;
  androidDirectory?: string;
  desktopDirectory?: string;
  appleDirectory?: string;
  // Where the Apple signing helpers (apple-testflight.py and others) live; this package does not ship them.
  appleHelpers?: string;
  // Adds a server check workflow when given.
  serverDirectory?: string;
  fixturesDirectory?: string;
  nodeVersion?: string;
  javaVersion?: string;
  macRunner?: string;
  windowsRunner?: string;
  wranglerMajor?: string;
  iconPath?: string;
  screenshots?: string;
  ipadScreenshots?: string;
  changelog?: string;
  description?: string;
  // Fields copied into the application manifest as they are, for example { minPlayers: 2, maxPlayers: 9 }.
  extra?: Record<string, unknown>;
  // Prefix of the environment variables carrying signing values; must equal the Android and Apple hosts' prefix (default: last word of the bundle or application id, in capitals).
  signingPrefix?: string;
  // Name of the Windows executable without .exe (default: the application name).
  productName?: string;
  iosDevices?: string[];
  macosMinimum?: string;
  tvosMinimum?: string;
}

export class SettingsError extends Error {}

const SLUG = /^[a-z][a-z0-9-]{0,63}$/;
const LABEL = /^[A-Za-z][A-Za-z0-9 -]{0,63}$/;
const QUALIFIED = /^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$/;
const ADDRESS = /^https?:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?$/;
const REPOSITORY = /^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9_-]+$/;
const RELEASE_ADDRESS = /^https:\/\/api\.github\.com\/repos\/[A-Za-z0-9-]+\/[A-Za-z0-9_-]+\/releases\/latest$/;
const DEFAULT_STORE = "https://ourapps.app";
const SECRET_REFERENCE = /^[A-Za-z][A-Za-z0-9_./-]{0,127}$/;
const PATH = /^[A-Za-z0-9_][A-Za-z0-9_./*-]*$/;
const VERSION = /^[0-9]{1,3}(\.[0-9]{1,3}){0,2}$/;
const RUNNER = /^[a-z][a-z0-9.-]{0,31}$/;

export interface ResolvedRelease {
  targets: Target[];
  applicationId: string;
  applicationName: string;
  storeAddress: string;
  manifestUrl: string | null;
  repository: string | null;
  storeCredentials: string | null;
  bundleIdentifier: string | null;
  androidSigning: string | null;
  webDeploy: string | null;
  hostingAccount: string | null;
}

export function parseTargets(value: string): Target[] {
  if (typeof value !== "string") throw new SettingsError("targets must be a string");
  const found: Target[] = [];
  for (const word of value.split(/\s+/).filter(Boolean)) {
    if (!(TARGETS as readonly string[]).includes(word)) throw new SettingsError(`unknown target ${word}; expected ${TARGETS.join(", ")}`);
    if (found.includes(word as Target)) throw new SettingsError(`target ${word} is listed twice`);
    found.push(word as Target);
  }
  if (!found.length) throw new SettingsError("targets must name at least one of " + TARGETS.join(", "));
  return TARGETS.filter((target) => found.includes(target));
}

// The repository secret name for a reference: capitals, digits and underscores.
export function secretName(reference: string): string {
  if (typeof reference !== "string" || !SECRET_REFERENCE.test(reference)) throw new SettingsError("a secret reference is the name of a secret (letters, digits, dot, slash, dash, underscore), never a value");
  return reference.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

export function validateSettings(settings: ReleaseSettings): ResolvedRelease {
  const targets = parseTargets(settings.targets);
  if (typeof settings.applicationId !== "string" || !SLUG.test(settings.applicationId)) throw new SettingsError("applicationId must be a slug");
  if (typeof settings.applicationName !== "string" || !LABEL.test(settings.applicationName)) throw new SettingsError("applicationName must be a label");
  const store = settings.storeAddress ?? DEFAULT_STORE;
  if (typeof store !== "string" || !ADDRESS.test(store.replace(/\/$/, ""))) throw new SettingsError("storeAddress must be an http or https address without a path");
  const storeAddress = store.replace(/\/$/, "");
  if (!storeAddress.startsWith("https://") && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(storeAddress)) throw new SettingsError("storeAddress must use https (http only for localhost)");
  const manifestUrl = given(settings.manifestUrl);
  if (manifestUrl !== null && !RELEASE_ADDRESS.test(manifestUrl)) throw new SettingsError("manifestUrl must be https://api.github.com/repos/<owner>/<name>/releases/latest");
  const repository = given(settings.repository);
  if (repository !== null && !REPOSITORY.test(repository)) throw new SettingsError("repository must be https://github.com/<owner>/<name>");
  const storeCredentials = reference(settings.storeCredentials);
  const apple = targets.some((target) => target === "ios" || target === "macos" || target === "tvos");
  let bundleIdentifier: string | null = null;
  if (settings.bundleIdentifier !== undefined && settings.bundleIdentifier !== "") {
    if (!QUALIFIED.test(settings.bundleIdentifier)) throw new SettingsError("bundleIdentifier must be lower-case words separated by dots");
    bundleIdentifier = settings.bundleIdentifier;
  } else if (apple) {
    throw new SettingsError("bundleIdentifier is required for ios, macos and tvos");
  }
  const androidSigning = reference(settings.androidSigning);
  if (androidSigning === null && targets.includes("android")) throw new SettingsError("androidSigning, the secret reference for the signing key, is required for android");
  const webDeploy = reference(settings.webDeploy);
  if (webDeploy !== null && !targets.includes("web")) throw new SettingsError("webDeploy is only for the web target");
  const hostingAccount = reference(settings.hostingAccount);
  const names = [storeCredentials, androidSigning, webDeploy, hostingAccount].filter((ref): ref is string => ref !== null).map(secretName);
  if (new Set(names).size !== names.length) throw new SettingsError("secret references must name different secrets");
  return { targets, applicationId: settings.applicationId, applicationName: settings.applicationName, storeAddress, manifestUrl, repository,
    storeCredentials, bundleIdentifier, androidSigning, webDeploy, hostingAccount };
}

// An optional text setting: unset, empty or "none" is null.
function given(value: string | undefined): string | null {
  if (value === undefined || value === "" || value === "none") return null;
  if (typeof value !== "string") throw new SettingsError("settings are text");
  return value;
}

function reference(value: string | undefined): string | null {
  const name = given(value);
  if (name !== null) secretName(name);
  return name;
}

// Template language: @@name@@ inserts a value; a line holding only @@if flag@@ ... @@end@@ keeps the lines between when the flag is set;
// @@if flag@@text@@end@@ on one line does the same inside a line. Unknown names are errors.
const TOKEN = /@@([A-Za-z][A-Za-z0-9]*)@@/g;

export function expand(template: string, flags: ReadonlySet<string>, values: Record<string, string>): string {
  const kept: string[] = [];
  const stack: boolean[] = [];
  for (const line of template.split("\n")) {
    const open = /^@@if ([A-Za-z][A-Za-z0-9]*)@@$/.exec(line);
    if (open) { stack.push(flagOf(open[1], flags)); continue; }
    if (line === "@@end@@") {
      if (!stack.length) throw new SettingsError("template has @@end@@ without @@if");
      stack.pop();
      continue;
    }
    if (stack.every(Boolean)) kept.push(line);
  }
  if (stack.length) throw new SettingsError("template has @@if without @@end@@");
  const text = kept.join("\n")
    .replace(/@@if ([A-Za-z][A-Za-z0-9]*)@@(.*?)@@end@@/g, (_all, flag: string, inner: string) => (flagOf(flag, flags) ? inner : ""));
  const filled = text.replace(TOKEN, (_all, name: string) => {
    if (!Object.hasOwn(values, name)) throw new SettingsError(`template names unknown value ${name}`);
    return values[name];
  });
  if (filled.includes("@@")) throw new SettingsError("template has a malformed @@ token");
  return filled;
}

function flagOf(name: string, flags: ReadonlySet<string>): boolean {
  if (!KNOWN_FLAGS.has(name)) throw new SettingsError(`template names unknown flag ${name}`);
  return flags.has(name);
}
const KNOWN_FLAGS = new Set<string>([...TARGETS, "apple", "cloudflare", "server", "store", "nostore"]);

function template(path: string): string {
  return readFileSync(new URL(`./templates/${path}.template`, import.meta.url), "utf8");
}

function path(name: string, value: string): string {
  if (typeof value !== "string" || !PATH.test(value) || value.split("/").includes("..")) throw new SettingsError(`${name} must be a plain relative path`);
  return value.replace(/\/$/, "");
}

export interface RenderResult {
  // Output path (relative to the application repository) to file text.
  files: Record<string, string>;
  // Files that must be executable.
  executable: string[];
  // Things the rendered files call that this package does not ship.
  notes: string[];
}

export function renderRelease(settings: ReleaseSettings, options: ReleaseOptions = {}): RenderResult {
  const resolved = validateSettings(settings);
  const has = (target: Target) => resolved.targets.includes(target);
  const apple = has("macos") || has("tvos");
  const o = {
    scripts: path("scriptsDirectory", options.scriptsDirectory ?? "components/ourapps-release/server/scripts"),
    web: path("webDirectory", options.webDirectory ?? "web"),
    android: path("androidDirectory", options.androidDirectory ?? "android"),
    desktop: path("desktopDirectory", options.desktopDirectory ?? "desktop"),
    apple: path("appleDirectory", options.appleDirectory ?? "ios"),
    appleHelpers: path("appleHelpers", options.appleHelpers ?? "scripts"),
    fixtures: path("fixturesDirectory", options.fixturesDirectory ?? "server/fixtures"),
    icon: path("iconPath", options.iconPath ?? "art/icon.png"),
    screenshots: path("screenshots", options.screenshots ?? "screenshots/*.png"),
    ipad: path("ipadScreenshots", options.ipadScreenshots ?? "screenshots/ipad"),
    changelog: path("changelog", options.changelog ?? "CHANGELOG.md"),
  };
  const node = options.nodeVersion ?? "22";
  const java = options.javaVersion ?? "17";
  const wrangler = options.wranglerMajor ?? "4";
  for (const [name, value] of [["nodeVersion", node], ["javaVersion", java], ["wranglerMajor", wrangler]] as const) if (!VERSION.test(value)) throw new SettingsError(`${name} must be a version number`);
  const macRunner = options.macRunner ?? "macos-26";
  const windowsRunner = options.windowsRunner ?? "windows-latest";
  for (const [name, value] of [["macRunner", macRunner], ["windowsRunner", windowsRunner]] as const) if (!RUNNER.test(value)) throw new SettingsError(`${name} must be a runner label`);
  const server = options.serverDirectory === undefined ? null : path("serverDirectory", options.serverDirectory);
  const productName = options.productName ?? resolved.applicationName;
  if (!LABEL.test(productName)) throw new SettingsError("productName must be a label");
  const prefix = options.signingPrefix ?? (resolved.bundleIdentifier ?? resolved.applicationId.replace(/-/g, ".")).split(".").pop()!.toUpperCase();
  if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(prefix)) throw new SettingsError("signingPrefix must be capitals, digits and underscores");
  for (const [name, value] of [["macosMinimum", options.macosMinimum ?? "14.0"], ["tvosMinimum", options.tvosMinimum ?? "17.0"]] as const) if (!VERSION.test(value)) throw new SettingsError(`${name} must be a version number`);

  const stored = resolved.storeCredentials !== null;
  const store = stored ? secretName(resolved.storeCredentials!) : "";
  const android = resolved.androidSigning ? secretName(resolved.androidSigning) : "";
  const cloudflare = resolved.webDeploy ? secretName(resolved.webDeploy) : "";
  // iPhone builds are signed and uploaded through the store, so without store credentials the release has no iOS job.
  const piped = resolved.targets.filter((target) => stored || target !== "ios");
  const built = (["android", "web", "ios"] as const).filter((target) => piped.includes(target));
  const flags = new Set<string>([...piped, ...(apple ? ["apple"] : []), ...(cloudflare ? ["cloudflare"] : []), ...(server ? ["server"] : []), stored ? "store" : "nostore"]);
  const desktopPlatforms = (["macos", "tvos", "windows"] as const).filter(has);
  const values: Record<string, string> = {
    targets: resolved.targets.join(" "), pipelineTargets: piped.join(" "), applicationId: resolved.applicationId, storeApi: resolved.storeAddress, scripts: o.scripts,
    secretsList: [...(stored ? [`store credentials ${resolved.storeCredentials}`] : []), ...(resolved.androidSigning ? [`Android signing ${resolved.androidSigning}`] : []), ...(resolved.webDeploy ? [`web hosting ${resolved.webDeploy}`] : [])].join(", ") || "none",
    storeCredentialsRef: resolved.storeCredentials ?? "", androidSigningRef: resolved.androidSigning ?? "", webDeployRef: resolved.webDeploy ?? "",
    storeAppId: `${store}_APP_ID`, storeToken: `${store}_BUILD_TOKEN`,
    androidKeystore: `${android}_KEYSTORE_BASE64`, androidKeystorePassword: `${android}_KEYSTORE_PASSWORD`, androidKeyAlias: `${android}_KEY_ALIAS`, androidKeyPassword: `${android}_KEY_PASSWORD`,
    cfToken: `${cloudflare}_API_TOKEN`, cfAccount: `${cloudflare}_ACCOUNT_ID`,
    signingPrefix: prefix, bundleId: resolved.bundleIdentifier ?? "", productName,
    webDir: o.web, androidDir: o.android, desktopDir: o.desktop, appleDir: o.apple, appleHelpers: o.appleHelpers, serverDir: server ?? "", fixturesDir: o.fixtures,
    node, java, wrangler, macRunner, windowsRunner, icon: o.icon, screenshots: o.screenshots, ipadScreenshots: o.ipad, changelog: o.changelog,
    needs: ["version", ...built, ...(built.includes("ios") ? ["testflight"] : [])].join(", "),
    anyOn: built.map((target) => `needs.version.outputs.${target} == 'true'`).join(" || ") || "false",
    webUrlExpression: has("web") ? "${{ needs.web.outputs.url }}" : "",
    desktopPlatforms: desktopPlatforms.join(", "),
    appleChoices: (["macos", "tvos"] as const).filter(has).join(", "),
    appleCondition: (["macos", "tvos"] as const).filter(has).map((target) => `inputs.platform == '${target}'`).join(" || ") || "false",
  };

  const files: Record<string, string> = {};
  const executable: string[] = [];
  const notes: string[] = [];
  const render = (name: string, output: string) => { files[output] = expand(template(name), flags, values); };

  const description = options.description ?? resolved.applicationName;
  if (typeof description !== "string" || !description.trim() || description.length > 300) throw new SettingsError("description must be 1 to 300 characters");
  const extra = options.extra ?? {};
  files["release/app.json"] = JSON.stringify({
    id: resolved.applicationId, name: resolved.applicationName, description,
    ...(resolved.bundleIdentifier ? { bundleId: resolved.bundleIdentifier } : {}),
    targets: resolved.targets, iosDevices: options.iosDevices ?? ["iphone", "ipad"],
    macos: { minOsVersion: options.macosMinimum ?? "14.0", architectures: ["arm64", "x64"] },
    tvos: { minOsVersion: options.tvosMinimum ?? "17.0" }, extra,
  }, null, 2) + "\n";

  if (built.length) render("workflows/release.yml", ".github/workflows/release.yml");
  else notes.push("release.yml is not rendered: none of android, web or (with store credentials) ios is a target, and the other targets publish only through verified packages.");
  if (has("web")) render("workflows/ci-web.yml", ".github/workflows/ci-web.yml");
  if (has("android")) render("workflows/ci-android.yml", ".github/workflows/ci-android.yml");
  if (server) render("workflows/ci-server.yml", ".github/workflows/ci-server.yml");
  if (desktopPlatforms.length) render("workflows/platform-packages.yml", ".github/workflows/platform-packages.yml");
  if (apple && !stored) notes.push("apple-testflight.yml and build-apple-testflight.sh are not rendered: the TestFlight export reserves builds, signs and uploads through the store, which has no credentials here. platform-packages.yml still builds unsigned Mac and Apple TV evidence.");
  if (has("ios") && !stored) notes.push("No iOS job is rendered: the iPhone build is signed and uploaded to TestFlight through the store, which has no credentials here.");
  if (apple && stored) {
    render("workflows/apple-testflight.yml", ".github/workflows/apple-testflight.yml");
    render("scripts/build-apple-testflight.sh", "scripts/build-apple-testflight.sh");
    executable.push("scripts/build-apple-testflight.sh");
    notes.push(`The Apple TestFlight workflow and script call ${o.appleHelpers}/apple-testflight.py, apple-testflight-upload.py and reserve-apple-build.py and ${o.apple}/build-platform.sh, which this package does not ship.`);
  }
  if (has("windows")) {
    render("scripts/build-windows.sh", "scripts/build-windows.sh");
    render("scripts/inspect-windows.ps1", "scripts/inspect-windows.ps1");
    executable.push("scripts/build-windows.sh");
    notes.push(`The Windows script runs the npm scripts check, package:win and smoke in ${o.web} and ${o.desktop} (the windows-desktop component provides the desktop ones).`);
  }
  if (has("ios")) notes.push(`The iOS job calls ${o.apple}/build-ipa.sh <version> <build> <profile> <output>, which this package does not ship; it must write app.ipa in the output folder.`);
  if (has("android")) notes.push(`The Android job needs ${o.android} to be a Gradle project whose release build reads ${prefix}_KEYSTORE, ${prefix}_KEYSTORE_PASSWORD, ${prefix}_KEY_ALIAS and ${prefix}_KEY_PASSWORD (android-host does).`);
  return { files, executable, notes };
}

export function writeRelease(directory: string, result: RenderResult): void {
  for (const [file, text] of Object.entries(result.files)) {
    const target = join(directory, file);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, text);
    if (result.executable.includes(file)) chmodSync(target, 0o755);
  }
}
