import { renderRelease, writeRelease, type ReleaseOptions, type ReleaseSettings } from "../implementation/server/render.ts";

// Build time: write the workflows, build scripts and the application description for the enabled targets.
// Returns what was written and what the rendered files expect the application repository to provide.
export function buildPipeline(directory: string, settings: ReleaseSettings, options: ReleaseOptions = {}): { written: string[]; notes: string[] } {
  const result = renderRelease(settings, options);
  writeRelease(directory, result);
  return { written: Object.keys(result.files).sort(), notes: result.notes };
}
