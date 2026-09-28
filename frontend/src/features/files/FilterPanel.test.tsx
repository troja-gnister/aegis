import {fireEvent, render, screen, within} from "@testing-library/react";
import {useRef, useState} from "react";
import {afterEach, describe, expect, it, vi} from "vitest";
import {FilterChips} from "./FilterChips";
import {FilterPanel} from "./FilterPanel";
import {
  describeFilterField,
  draftFromFilters,
  emptyFilterDraft,
  filtersFromDraft,
  localDayStart,
  parseSizeBytes,
  removeFilter,
} from "./filter-state";
import {setTestTimeZone} from "./test-fixtures";
import type {FileFilters} from "./types";

const restorers: Array<() => void> = [];

function timeZone(zone: string) {
  restorers.push(setTestTimeZone(zone));
}

afterEach(() => {
  while (restorers.length > 0) restorers.pop()!();
});

function applied(draft: Parameters<typeof filtersFromDraft>[0]): FileFilters {
  const result = filtersFromDraft(draft);
  if (!result.ok) throw new Error(`unexpected validation errors ${JSON.stringify(result.errors)}`);
  return result.filters;
}

function PanelHarness({initial = {v: 1} as FileFilters, onApply = vi.fn()}: {
  initial?: FileFilters;
  onApply?: (filters: FileFilters) => void;
}) {
  const [value, setValue] = useState<FileFilters>(initial);
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button ref={opener} type="button" onClick={() => setOpen(true)}>Filters</button>
      <output aria-label="Applied filters">{JSON.stringify(value)}</output>
      <FilterPanel
        value={value}
        open={open}
        onApply={(next) => {
          onApply(next);
          setValue(next);
          setOpen(false);
        }}
        onCancel={() => setOpen(false)}
      />
    </>
  );
}

describe("filter state", () => {
  it("removes exactly one filter field without mutating the applied filters", () => {
    const filters: FileFilters = {v: 1, kind: ["file"], prefix: "IMG_", size: {unknown: true}};
    const result = removeFilter(filters, "prefix");
    expect(result).toEqual({v: 1, kind: ["file"], size: {unknown: true}});
    expect(filters.prefix).toBe("IMG_");
  });

  it("combines values within a group with OR and different groups with AND", () => {
    const filters = applied({
      ...emptyFilterDraft(),
      kind: ["file", "directory"],
      type: ["jpg", "__unknown__"],
      extraTypes: ".UNKNOWN, raw",
      availability: ["present", "missing"],
    });
    expect(filters).toEqual({
      v: 1,
      kind: ["directory", "file"],
      type: ["jpg", "__unknown__", "unknown", "raw"],
      availability: ["present", "missing"],
    });
    expect(describeFilterField(filters, "kind")).toBe("Kind: Directory or File");
    expect(describeFilterField(filters, "type")).toBe("Type: .jpg or no extension or .unknown or .raw");
    expect(describeFilterField(filters, "availability")).toBe("Last indexed state: Present or Missing");
  });

  it("keeps the reserved unknown-type token distinct from a literal .unknown extension", () => {
    const literal = applied({...emptyFilterDraft(), extraTypes: "unknown"});
    const reserved = applied({...emptyFilterDraft(), type: ["__unknown__"]});
    expect(literal.type).toEqual(["unknown"]);
    expect(reserved.type).toEqual(["__unknown__"]);
    expect(draftFromFilters(literal).extraTypes).toBe("unknown");
    expect(draftFromFilters(literal).type).toEqual([]);
    expect(draftFromFilters(reserved).type).toEqual(["__unknown__"]);
    const typed = filtersFromDraft({...emptyFilterDraft(), extraTypes: "__unknown__"});
    expect(typed.ok).toBe(false);
  });

  it("parses decimal size inputs exactly and rejects fractional bytes or overflow", () => {
    expect(parseSizeBytes("1.5", "MB")).toBe("1500000");
    expect(parseSizeBytes("18446744073709551615", "B")).toBe("18446744073709551615");
    expect(parseSizeBytes("0.0015", "KB")).toBeNull();
    expect(parseSizeBytes("18446744073709551616", "B")).toBeNull();
    expect(parseSizeBytes("1e3", "B")).toBeNull();
    expect(parseSizeBytes("-1", "B")).toBeNull();
  });

  it("validates size ranges and explicit unknown metadata", () => {
    expect(applied({...emptyFilterDraft(), sizeMin: "2", sizeMinUnit: "KB", sizeMax: "3", sizeMaxUnit: "MB"}).size)
      .toEqual({min: "2000", max: "3000000"});
    expect(applied({...emptyFilterDraft(), sizeUnknown: true}).size).toEqual({unknown: true});
    expect(applied({...emptyFilterDraft(), modifiedUnknown: true}).modified).toEqual({unknown: true});
    const inverted = filtersFromDraft({...emptyFilterDraft(), sizeMin: "5", sizeMax: "4"});
    expect(inverted).toMatchObject({ok: false, errors: {size: expect.stringMatching(/minimum/i)}});
    const mixed = filtersFromDraft({...emptyFilterDraft(), sizeMin: "5", sizeUnknown: true});
    expect(mixed).toMatchObject({ok: false, errors: {size: expect.stringMatching(/unknown/i)}});
    const mixedDate = filtersFromDraft({...emptyFilterDraft(), modifiedFrom: "2026-01-01", modifiedUnknown: true});
    expect(mixedDate).toMatchObject({ok: false, errors: {modified: expect.stringMatching(/unknown/i)}});
  });

  it("maps local day boundaries to offset-qualified instants across DST", () => {
    timeZone("America/New_York");
    expect(localDayStart("2026-03-08")).toBe("2026-03-08T00:00:00-05:00");
    expect(localDayStart("2026-03-09")).toBe("2026-03-09T00:00:00-04:00");
    const filters = applied({...emptyFilterDraft(), modifiedFrom: "2026-03-08", modifiedTo: "2026-03-08"});
    expect(filters.modified).toEqual({
      from: "2026-03-08T00:00:00-05:00",
      before: "2026-03-09T00:00:00-04:00",
    });
    const draft = draftFromFilters(filters);
    expect(draft.modifiedFrom).toBe("2026-03-08");
    expect(draft.modifiedTo).toBe("2026-03-08");
    timeZone("Asia/Tokyo");
    expect(localDayStart("2026-03-08")).toBe("2026-03-08T00:00:00+09:00");
  });

  it("rejects a nonexistent local midnight and invalid calendar dates instead of shifting them", () => {
    timeZone("America/Santiago");
    expect(localDayStart("2026-09-06")).toBeNull();
    expect(localDayStart("2026-09-05")).toBe("2026-09-05T00:00:00-04:00");
    expect(localDayStart("2026-02-30")).toBeNull();
    expect(localDayStart("26-02-03")).toBeNull();
    const gap = filtersFromDraft({...emptyFilterDraft(), modifiedFrom: "2026-09-06"});
    expect(gap).toMatchObject({ok: false, errors: {modified: expect.stringMatching(/does not exist/i)}});
    const inverted = filtersFromDraft({...emptyFilterDraft(), modifiedFrom: "2026-09-10", modifiedTo: "2026-09-09"});
    expect(inverted).toMatchObject({ok: false, errors: {modified: expect.stringMatching(/end date/i)}});
    const outOfRange = filtersFromDraft({...emptyFilterDraft(), modifiedFrom: "2300-01-01"});
    expect(outOfRange.ok).toBe(false);
  });

  it("enforces 32 selections per field and the shared request budget", () => {
    const tooMany = Array.from({length: 33}, (_, index) => `x${index}`).join(",");
    expect(filtersFromDraft({...emptyFilterDraft(), extraTypes: tooMany}))
      .toMatchObject({ok: false, errors: {type: expect.stringMatching(/32/)}});
    expect(filtersFromDraft({...emptyFilterDraft(), prefix: "é".repeat(1025)}))
      .toMatchObject({ok: false, errors: {prefix: expect.any(String)}});
    // ASCII-escaped JSON budget: 1,400 BMP characters encode to 8,400 bytes.
    expect(filtersFromDraft({...emptyFilterDraft(), prefix: "é".repeat(1000) + "\u{1f600}".repeat(200)}))
      .toMatchObject({ok: false});
    expect(applied({...emptyFilterDraft(), prefix: ""})).toEqual({v: 1});
  });
});

describe("FilterPanel", () => {
  it("does not query while editing or after Cancel", async () => {
    const apply = vi.fn();
    const cancel = vi.fn();
    render(<FilterPanel value={{v: 1}} open onApply={apply} onCancel={cancel} />);
    fireEvent.change(screen.getByLabelText("Filename starts with"), {target: {value: "IMG_"}});
    expect(apply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", {name: "Cancel"}));
    expect(cancel).toHaveBeenCalledOnce();
    expect(apply).not.toHaveBeenCalled();
  });

  it("renders a labelled modal dialog with grouped, named controls", () => {
    timeZone("Europe/Berlin");
    render(<FilterPanel value={{v: 1}} open onApply={vi.fn()} onCancel={vi.fn()} />);
    const dialog = screen.getByRole("dialog", {name: "Filter files"});
    expect(dialog.tagName).toBe("DIALOG");
    expect(within(dialog).getByRole("group", {name: "Entry kind"})).toBeVisible();
    expect(within(dialog).getByRole("group", {name: "File type"})).toBeVisible();
    expect(within(dialog).getByRole("group", {name: "Last indexed state"})).toBeVisible();
    expect(within(dialog).getByRole("group", {name: "File size"})).toBeVisible();
    expect(within(dialog).getByRole("group", {name: "File modified date"})).toBeVisible();
    expect(within(dialog).getByRole("checkbox", {name: "No extension (unknown type)"})).toBeVisible();
    expect(within(dialog).getByLabelText("Other extensions")).toBeVisible();
    expect(within(dialog).getByLabelText("Minimum size")).toHaveAttribute("inputmode", "decimal");
    expect(within(dialog).getByText(/Europe\/Berlin/)).toBeVisible();
    expect(within(dialog).getByText(/not photo capture time/i)).toBeVisible();
    expect(within(dialog).getByText(/ancestor.*currently unavailable/i)).toBeVisible();
    expect(within(dialog).getByText(/any selected value.*every group/i)).toBeVisible();
    expect(within(dialog).getByRole("button", {name: "Apply filters"})).toBeVisible();
    expect(within(dialog).getByRole("button", {name: "Clear all"})).toBeVisible();
  });

  it("applies a validated draft once and restores focus to Filters", () => {
    const onApply = vi.fn();
    render(<PanelHarness onApply={onApply} />);
    const opener = screen.getByRole("button", {name: "Filters"});
    opener.focus();
    fireEvent.click(opener);
    fireEvent.click(screen.getByRole("checkbox", {name: "File"}));
    fireEvent.click(screen.getByRole("checkbox", {name: "Directory"}));
    fireEvent.change(screen.getByLabelText("Filename starts with"), {target: {value: "IMG_"}});
    fireEvent.click(screen.getByRole("button", {name: "Apply filters"}));
    expect(onApply).toHaveBeenCalledOnce();
    expect(onApply).toHaveBeenCalledWith({v: 1, kind: ["directory", "file"], prefix: "IMG_"});
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("shows validation errors without applying and keeps the draft editable", () => {
    const onApply = vi.fn();
    render(<PanelHarness onApply={onApply} />);
    fireEvent.click(screen.getByRole("button", {name: "Filters"}));
    fireEvent.change(screen.getByLabelText("Minimum size"), {target: {value: "10"}});
    fireEvent.change(screen.getByLabelText("Maximum size"), {target: {value: "2"}});
    fireEvent.click(screen.getByRole("button", {name: "Apply filters"}));
    expect(onApply).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/minimum size/i);
    expect(screen.getByLabelText("Minimum size")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Minimum size")).toHaveFocus();
    fireEvent.change(screen.getByLabelText("Maximum size"), {target: {value: "20"}});
    fireEvent.click(screen.getByRole("button", {name: "Apply filters"}));
    expect(onApply).toHaveBeenCalledWith({v: 1, size: {min: "10", max: "20"}});
  });

  it("discards the draft on Escape and restores focus without changing applied filters", () => {
    const onApply = vi.fn();
    render(<PanelHarness initial={{v: 1, prefix: "DSC"}} onApply={onApply} />);
    const opener = screen.getByRole("button", {name: "Filters"});
    opener.focus();
    fireEvent.click(opener);
    const prefix = screen.getByLabelText("Filename starts with");
    expect(prefix).toHaveValue("DSC");
    fireEvent.change(prefix, {target: {value: "IMG_"}});
    fireEvent.keyDown(screen.getByRole("dialog"), {key: "Escape"});
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(onApply).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Applied filters")).toHaveTextContent('{"v":1,"prefix":"DSC"}');

    fireEvent.click(opener);
    expect(screen.getByLabelText("Filename starts with")).toHaveValue("DSC");
    fireEvent.click(screen.getByRole("button", {name: "Cancel"}));
    expect(opener).toHaveFocus();
  });

  it("handles a native dialog cancel request as Cancel", () => {
    const cancel = vi.fn();
    render(<FilterPanel value={{v: 1}} open onApply={vi.fn()} onCancel={cancel} />);
    const event = new Event("cancel", {cancelable: true});
    screen.getByRole("dialog").dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("clears the draft only until Apply", () => {
    const onApply = vi.fn();
    render(<PanelHarness initial={{v: 1, kind: ["file"], size: {unknown: true}}} onApply={onApply} />);
    fireEvent.click(screen.getByRole("button", {name: "Filters"}));
    expect(screen.getByRole("checkbox", {name: "File"})).toBeChecked();
    fireEvent.click(screen.getByRole("button", {name: "Clear all"}));
    expect(screen.getByRole("checkbox", {name: "File"})).not.toBeChecked();
    expect(screen.getByLabelText("Applied filters")).toHaveTextContent('"kind":["file"]');
    fireEvent.click(screen.getByRole("button", {name: "Apply filters"}));
    expect(onApply).toHaveBeenCalledWith({v: 1});
  });

  it("offers no content, viewer, editor, or destructive action", () => {
    render(<FilterPanel value={{v: 1}} open onApply={vi.fn()} onCancel={vi.fn()} />);
    const names = screen.getAllByRole("button").map((button) => button.textContent);
    expect(names).toEqual(["Clear all", "Cancel", "Apply filters"]);
  });
});

describe("FilterChips", () => {
  it("renders removable native buttons with full accessible labels", () => {
    const onChange = vi.fn();
    const value: FileFilters = {v: 1, kind: ["file"], type: ["__unknown__", "unknown"], prefix: "IMG_"};
    render(<FilterChips value={value} onChange={onChange} />);
    const chip = screen.getByRole("button", {name: "Remove filter Type: no extension or .unknown"});
    expect(chip.tagName).toBe("BUTTON");
    expect(screen.getByRole("button", {name: 'Remove filter Filename starts with "IMG_"'})).toBeVisible();
    fireEvent.click(chip);
    expect(onChange).toHaveBeenLastCalledWith({v: 1, kind: ["file"], prefix: "IMG_"});
    fireEvent.click(screen.getByRole("button", {name: "Clear all filters"}));
    expect(onChange).toHaveBeenLastCalledWith({v: 1});
  });

  it("describes size, modified and unknown metadata filters explicitly", () => {
    render(
      <FilterChips
        value={{
          v: 1,
          size: {min: "1500000", max: "18446744073709551615"},
          modified: {from: "2026-03-08T00:00:00-05:00", before: "2026-03-09T00:00:00-04:00"},
        }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", {
      name: "Remove filter Size: 1,500,000 to 18,446,744,073,709,551,615 bytes",
    })).toBeVisible();
    expect(screen.getByRole("button", {
      name: "Remove filter Modified: from 2026-03-08 00:00 UTC-05:00 until before 2026-03-09 00:00 UTC-04:00",
    })).toBeVisible();
  });

  it("renders nothing without active filters", () => {
    const {container} = render(<FilterChips value={{v: 1}} onChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
    expect(describeFilterField({v: 1, size: {unknown: true}}, "size")).toBe("Size: unknown");
    expect(describeFilterField({v: 1, modified: {unknown: true}}, "modified")).toBe("Modified: unknown");
  });
});
