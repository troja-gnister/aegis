import {describe, expect, it} from "vitest";
import {cameraText, displayDimensions, formatDimensions, formatDuration, formatRecordedTime} from "./format";

describe("media formatting", () => {
  it.each([
    [0, "0:00"],
    [999, "0:00"],
    [61_500, "1:01"],
    [600_000, "10:00"],
    [3_723_000, "1:02:03"],
    [36_000_000, "10:00:00"],
  ])("formats %i ms as %s", (durationMs, text) => {
    expect(formatDuration(durationMs)).toBe(text);
  });

  it("shows the server's display dimensions as reported, never swapped", () => {
    expect(displayDimensions({width: 4032, height: 3024})).toEqual({width: 4032, height: 3024});
    expect(displayDimensions({width: 3024, height: 4032})).toEqual({width: 3024, height: 4032});
    expect(displayDimensions({width: null, height: 3024})).toBeNull();
    expect(displayDimensions({width: 4032, height: null})).toBeNull();
    expect(formatDimensions({width: 4032, height: 3024})).toBe("4,032 × 3,024 pixels");
  });

  it("names a camera without repeating the make", () => {
    expect(cameraText({cameraMake: "Synthetic Optics", cameraModel: "Synthetic Optics Model 7"}))
      .toBe("Synthetic Optics Model 7");
    expect(cameraText({cameraMake: "Synthetic", cameraModel: "Model 7"})).toBe("Synthetic Model 7");
    expect(cameraText({cameraMake: "Synthetic", cameraModel: null})).toBe("Synthetic");
    expect(cameraText({cameraMake: null, cameraModel: "Model 7"})).toBe("Model 7");
    expect(cameraText({cameraMake: "  ", cameraModel: null})).toBeNull();
    expect(cameraText({cameraMake: null, cameraModel: null})).toBeNull();
  });

  it("shows a recorded time as its own wall clock and offset, not the viewer's zone", () => {
    const wallClock = (hour: number) => new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium", timeStyle: "medium", timeZone: "UTC",
    }).format(Date.UTC(2026, 11, 24, hour, 30, 0));

    expect(formatRecordedTime("2026-12-24T18:30:00+09:00")).toBe(`${wallClock(18)} (UTC+09:00)`);
    expect(formatRecordedTime("2026-12-24T18:30:00.123456-05:30")).toBe(`${wallClock(18)} (UTC-05:30)`);
    expect(formatRecordedTime("2026-12-24T07:30:00Z")).toBe(`${wallClock(7)} (UTC)`);
    expect(formatRecordedTime("2026-12-24T07:30:00+00:00")).toBe(`${wallClock(7)} (UTC)`);
    expect(formatRecordedTime("2026-12-24T07:30:00")).toBeNull();
  });
});
