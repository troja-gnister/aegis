import {useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent} from "react";
import {MAX_SEARCH_CHARACTERS, parseSearch, SEARCH_HINT} from "./filter-state";

/** Typing pauses this long before a search is applied. */
export const SEARCH_DEBOUNCE_MS = 300;

export type NameSearchProps = {
  /** The applied search text, if any. */
  applied: string | undefined;
  /** Apply *text* as the search; empty text removes it. */
  onSearch(text: string): void;
};

/** Whether a "/" typed at *target* belongs to the page rather than to a field or a dialog. */
function typedOnPage(target: EventTarget | null): boolean {
  return !(target instanceof HTMLElement) || !(
    target.isContentEditable || target.closest("input, textarea, select, dialog, [role='dialog']")
  );
}

/**
 * The Files toolbar's name search. Text applies after a pause in typing, once it has
 * three characters with letters or digits; clearing it removes the search. "/" focuses
 * it from the page and Escape clears it.
 */
export function NameSearch({applied, onSearch}: NameSearchProps) {
  const id = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const onSearchRef = useRef(onSearch);
  const [text, setText] = useState(applied ?? "");
  const {text: wanted, tooShort} = parseSearch(text);
  const current = applied ?? "";

  useEffect(() => {
    onSearchRef.current = onSearch;
  });

  useEffect(() => {
    // A search changed elsewhere (a chip, Clear all, Back): show it, unless the box
    // already holds the same search.
    setText((value) => parseSearch(value).text === current ? value : current);
  }, [current]);

  useEffect(() => {
    if (tooShort || wanted === current) return;
    const timer = setTimeout(() => onSearchRef.current(wanted), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [current, tooShort, wanted]);

  useEffect(() => {
    const focusSearch = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "/" || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
      if (!typedOnPage(event.target)) return;
      event.preventDefault();
      inputRef.current?.focus();
    };
    document.addEventListener("keydown", focusSearch);
    return () => document.removeEventListener("keydown", focusSearch);
  }, []);

  const clear = () => {
    setText("");
    if (current !== "") onSearch("");
    inputRef.current?.focus();
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!tooShort && wanted !== current) onSearch(wanted);
  };

  const keyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Escape" || text === "") return;
    event.preventDefault();
    clear();
  };

  return (
    <form className="name-search" role="search" aria-labelledby={`${id}-label`} onSubmit={submit}>
      <label id={`${id}-label`} className="visually-hidden" htmlFor={`${id}-input`}>
        Search names
      </label>
      <input
        id={`${id}-input`}
        ref={inputRef}
        className="name-search__input"
        type="search"
        enterKeyHint="search"
        autoComplete="off"
        autoCapitalize="none"
        spellCheck={false}
        maxLength={MAX_SEARCH_CHARACTERS}
        placeholder="Search names"
        aria-describedby={`${id}-hint`}
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={keyDown}
      />
      {text !== "" ? (
        <button className="name-search__clear interactive" type="button" aria-label="Clear search" onClick={clear}>
          <span aria-hidden="true">×</span>
        </button>
      ) : null}
      <p id={`${id}-hint`} className="name-search__hint" role="status">
        {tooShort ? SEARCH_HINT : ""}
      </p>
    </form>
  );
}
