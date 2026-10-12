import {mkdir, readdir, readFile, writeFile} from "node:fs/promises";
import {join} from "node:path";
import type {FullConfig, TestInfo} from "@playwright/test";

// One record per browser project that ran the video journey, in the run's output directory
// (which Playwright empties when a run starts). Booleans only: no names, URLs or values.
const RECORD = /^aegis-video-playback-[a-z0-9-]+\.json$/;

/** Records whether this project's video journey proved real playback. */
export async function recordPlayback(testInfo: TestInfo, played: boolean): Promise<void> {
  await mkdir(testInfo.project.outputDir, {recursive: true});
  await writeFile(
    join(testInfo.project.outputDir, `aegis-video-playback-${testInfo.project.name}.json`),
    JSON.stringify({played}),
  );
}

/**
 * Global teardown: a browser may lack a codec and take the journey's asserted "cannot play"
 * branch, but when any project ran the video journey, at least one of them must have proved
 * that a video really plays and seeks. Otherwise the run fails here.
 */
export default async function requirePlaybackInSomeBrowser(config: FullConfig): Promise<void> {
  const played: boolean[] = [];
  for (const directory of new Set(config.projects.map((project) => project.outputDir))) {
    let names: string[];
    try {
      names = await readdir(directory);
    } catch {
      continue;
    }
    for (const name of names.filter((candidate) => RECORD.test(candidate))) {
      const record = JSON.parse(await readFile(join(directory, name), "utf8")) as {played?: unknown};
      played.push(record.played === true);
    }
  }
  if (played.length > 0 && !played.includes(true)) {
    throw new Error("No browser project in this run proved video playback.");
  }
}
