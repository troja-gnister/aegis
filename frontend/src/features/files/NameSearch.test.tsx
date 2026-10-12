import {act, fireEvent, render, screen} from "@testing-library/react";
import {useState} from "react";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {NameSearch, SEARCH_DEBOUNCE_MS} from "./NameSearch";

function Harness({initial, onSearch}: {initial?: string; onSearch(text: string): void}) {
  const [applied, setApplied] = useState(initial);
  return (
    <>
      <NameSearch
        applied={applied}
        onSearch={(text) => {
          onSearch(text);
          setApplied(text === "" ? undefined : text);
        }}
      />
      <button type="button" onClick={() => setApplied(undefined)}>Remove search chip</button>
      <input aria-label="Other field" />
      <dialog open aria-label="A dialog"><button type="button">Inside dialog</button></dialog>
    </>
  );
}

function type(value: string) {
  fireEvent.change(screen.getByRole("searchbox", {name: "Search names"}), {target: {value}});
}

function pause() {
  act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS));
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("NameSearch", () => {
  it("is a labelled search landmark that applies text after a pause", () => {
    const onSearch = vi.fn();
    render(<Harness onSearch={onSearch} />);
    expect(screen.getByRole("search", {name: "Search names"})).toBeVisible();
    expect(screen.queryByRole("button", {name: "Clear search"})).not.toBeInTheDocument();

    type("t");
    type("tr");
    type("tri");
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS - 1));
    expect(onSearch).not.toHaveBeenCalled();
    type(" trip ");
    pause();
    expect(onSearch).toHaveBeenCalledTimes(1);
    expect(onSearch).toHaveBeenCalledWith("trip");
    expect(screen.getByRole("button", {name: "Clear search"})).toBeVisible();
  });

  it("asks for three characters and never searches shorter text", () => {
    const onSearch = vi.fn();
    render(<Harness onSearch={onSearch} />);
    const box = screen.getByRole("searchbox", {name: "Search names"});
    type("tr");
    pause();
    expect(onSearch).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Type at least 3 letters or digits");
    expect(box).toHaveAccessibleDescription("Type at least 3 letters or digits");
    // Three characters without a letter or digit cannot be looked up either.
    type("---");
    pause();
    expect(onSearch).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Type at least 3 letters or digits");
    fireEvent.submit(box.closest("form")!);
    expect(onSearch).not.toHaveBeenCalled();
    type("trip");
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("searches at once on Enter and clears with the button, keeping focus in the box", () => {
    const onSearch = vi.fn();
    render(<Harness onSearch={onSearch} />);
    const box = screen.getByRole("searchbox", {name: "Search names"});
    type("trip");
    fireEvent.submit(box.closest("form")!);
    expect(onSearch).toHaveBeenLastCalledWith("trip");
    pause();
    expect(onSearch).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", {name: "Clear search"}));
    expect(onSearch).toHaveBeenLastCalledWith("");
    expect(box).toHaveValue("");
    expect(box).toHaveFocus();
  });

  it("clears on Escape while focused", () => {
    const onSearch = vi.fn();
    render(<Harness initial="trip" onSearch={onSearch} />);
    const box = screen.getByRole("searchbox", {name: "Search names"});
    expect(box).toHaveValue("trip");
    box.focus();
    fireEvent.keyDown(box, {key: "Escape"});
    expect(box).toHaveValue("");
    expect(onSearch).toHaveBeenCalledWith("");
    // A short draft that was never applied clears without a search.
    type("tr");
    fireEvent.keyDown(box, {key: "Escape"});
    expect(box).toHaveValue("");
    expect(onSearch).toHaveBeenCalledTimes(1);
  });

  it("follows a search removed elsewhere", () => {
    render(<Harness initial="trip" onSearch={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", {name: "Remove search chip"}));
    expect(screen.getByRole("searchbox", {name: "Search names"})).toHaveValue("");
  });

  it("focuses on / from the page but not from a field or a dialog", () => {
    render(<Harness onSearch={vi.fn()} />);
    const box = screen.getByRole("searchbox", {name: "Search names"});
    const other = screen.getByRole("textbox", {name: "Other field"});
    other.focus();
    fireEvent.keyDown(other, {key: "/"});
    expect(other).toHaveFocus();
    const inDialog = screen.getByRole("button", {name: "Inside dialog"});
    inDialog.focus();
    fireEvent.keyDown(inDialog, {key: "/"});
    expect(inDialog).toHaveFocus();
    fireEvent.keyDown(document.body, {key: "/", ctrlKey: true});
    expect(box).not.toHaveFocus();
    fireEvent.keyDown(document.body, {key: "/"});
    expect(box).toHaveFocus();
  });
});
