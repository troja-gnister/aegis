import {
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type SyntheticEvent,
} from "react";
import {
  AVAILABILITY_OPTIONS,
  COMMON_TYPES,
  draftFromFilters,
  emptyFilterDraft,
  filtersFromDraft,
  KIND_OPTIONS,
  localTimeZoneName,
  SIZE_UNITS,
  UNKNOWN_TYPE,
  type FilterDraft,
  type FilterDraftErrors,
  type SizeUnit,
} from "./filter-state";
import type {FileFilters} from "./types";

export type FilterPanelProps = {
  value: FileFilters;
  open: boolean;
  onApply(filters: FileFilters): void;
  onCancel(): void;
};

export function FilterPanel({value, open, onApply, onCancel}: FilterPanelProps) {
  if (!open) return null;
  return <FilterDialog value={value} onApply={onApply} onCancel={onCancel} />;
}

const ERROR_TARGETS: readonly (keyof FilterDraftErrors)[] = [
  "kind", "type", "size", "modified", "availability", "prefix", "form",
];

function toggle<T>(values: T[], value: T, checked: boolean): T[] {
  return checked ? [...values.filter((item) => item !== value), value] : values.filter((item) => item !== value);
}

function FilterDialog({value, onApply, onCancel}: Omit<FilterPanelProps, "open">) {
  const id = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const fieldRefs = useRef(new Map<keyof FilterDraftErrors, HTMLElement>());
  // Each opening starts from the applied filters; nothing here reaches the query until Apply.
  const [draft, setDraft] = useState<FilterDraft>(() => draftFromFilters(value));
  const [errors, setErrors] = useState<FilterDraftErrors>({});
  const [timeZone] = useState(localTimeZoneName);

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (dialog && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }
    return () => {
      if (dialog?.open) {
        if (typeof dialog.close === "function") dialog.close();
        else dialog.removeAttribute("open");
      }
      if (opener?.isConnected) opener.focus({preventScroll: true});
    };
  }, []);

  const update = (changes: Partial<FilterDraft>) => setDraft((current) => ({...current, ...changes}));
  const errorId = (field: keyof FilterDraftErrors) => `${id}-${field}-error`;
  const describedBy = (field: keyof FilterDraftErrors, hint?: string) =>
    [hint, errors[field] ? errorId(field) : null].filter(Boolean).join(" ") || undefined;
  const register = (field: keyof FilterDraftErrors) => (node: HTMLElement | null) => {
    if (node) fieldRefs.current.set(field, node);
    else fieldRefs.current.delete(field);
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const result = filtersFromDraft(draft);
    if (!result.ok) {
      setErrors(result.errors);
      const first = ERROR_TARGETS.find((field) => result.errors[field] !== undefined);
      if (first) fieldRefs.current.get(first)?.focus();
      return;
    }
    onApply(result.filters);
  };

  const cancelRequest = (event: SyntheticEvent) => {
    event.preventDefault();
    onCancel();
  };

  const keyDown = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== "Escape") return;
    // Handling Escape here prevents the browser from also issuing a cancel event.
    event.preventDefault();
    event.stopPropagation();
    onCancel();
  };

  const errorText = (field: keyof FilterDraftErrors) => errors[field] ? (
    <p className="filter-panel__error" id={errorId(field)}>{errors[field]}</p>
  ) : null;
  const errorCount = Object.keys(errors).length;

  return (
    <dialog
      ref={dialogRef}
      className="filter-panel"
      aria-labelledby={`${id}-title`}
      aria-describedby={`${id}-semantics`}
      onCancel={cancelRequest}
      onKeyDown={keyDown}
    >
      <form className="filter-panel__form" noValidate onSubmit={submit}>
        <header className="filter-panel__header">
          <h2 id={`${id}-title`}>Filter files</h2>
          <p id={`${id}-semantics`} className="filter-panel__hint">
            Within a group, entries match any selected value; every group you set must match.
          </p>
        </header>
        {errorCount > 0 ? (
          <p className="notice notice--error" role="alert">
            {errors.form ?? "Check the highlighted filters: "}
            {errors.form ? null : ERROR_TARGETS.map((field) => errors[field]).filter(Boolean).join(" ")}
          </p>
        ) : null}

        <div className="filter-panel__field">
          <label htmlFor={`${id}-prefix`}>Filename starts with</label>
          <input
            id={`${id}-prefix`}
            ref={register("prefix")}
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={draft.prefix}
            aria-invalid={errors.prefix ? true : undefined}
            aria-describedby={describedBy("prefix", `${id}-prefix-hint`)}
            onChange={(event) => update({prefix: event.target.value})}
          />
          <small id={`${id}-prefix-hint`}>Literal text in this folder; letter case is ignored.</small>
          {errorText("prefix")}
        </div>

        <fieldset className="filter-panel__group" ref={register("kind")} tabIndex={-1}>
          <legend>Entry kind</legend>
          <div className="filter-panel__choices">
            {KIND_OPTIONS.map((option) => (
              <label key={option.value} className="filter-panel__choice">
                <input
                  type="checkbox"
                  checked={draft.kind.includes(option.value)}
                  onChange={(event) => update({kind: toggle(draft.kind, option.value, event.target.checked)})}
                />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className="filter-panel__group" aria-describedby={describedBy("type")}>
          <legend>File type</legend>
          <div className="filter-panel__choices">
            {COMMON_TYPES.map((type) => (
              <label key={type} className="filter-panel__choice">
                <input
                  type="checkbox"
                  checked={draft.type.includes(type)}
                  onChange={(event) => update({type: toggle(draft.type, type, event.target.checked)})}
                />
                <span>.{type}</span>
              </label>
            ))}
            <label className="filter-panel__choice">
              <input
                type="checkbox"
                checked={draft.type.includes(UNKNOWN_TYPE)}
                onChange={(event) => update({type: toggle(draft.type, UNKNOWN_TYPE, event.target.checked)})}
              />
              <span>No extension (unknown type)</span>
            </label>
          </div>
          <div className="filter-panel__field">
            <label htmlFor={`${id}-types`}>Other extensions</label>
            <input
              id={`${id}-types`}
              ref={register("type")}
              type="text"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={draft.extraTypes}
              aria-invalid={errors.type ? true : undefined}
              aria-describedby={describedBy("type", `${id}-types-hint`)}
              onChange={(event) => update({extraTypes: event.target.value})}
            />
            <small id={`${id}-types-hint`}>
              Separate with commas, for example raw, cr2. A typed “unknown” means the literal .unknown extension.
            </small>
          </div>
          {errorText("type")}
        </fieldset>

        <fieldset className="filter-panel__group" aria-describedby={`${id}-availability-hint`}>
          <legend>Last indexed state</legend>
          <p id={`${id}-availability-hint`} className="filter-panel__hint">
            Matches the state recorded by the last index scan. An ancestor change can make a matching
            entry currently unavailable, so results may show a different current availability.
          </p>
          <div className="filter-panel__choices">
            {AVAILABILITY_OPTIONS.map((option) => (
              <label key={option.value} className="filter-panel__choice">
                <input
                  type="checkbox"
                  checked={draft.availability.includes(option.value)}
                  onChange={(event) => update({
                    availability: toggle(draft.availability, option.value, event.target.checked),
                  })}
                />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className="filter-panel__group" aria-describedby={describedBy("size", `${id}-size-hint`)}>
          <legend>File size</legend>
          <p id={`${id}-size-hint`} className="filter-panel__hint">Both limits are inclusive.</p>
          {([["Minimum size", "sizeMin", "sizeMinUnit"], ["Maximum size", "sizeMax", "sizeMaxUnit"]] as const)
            .map(([label, field, unitField]) => (
              <div key={field} className="filter-panel__size">
                <label className="filter-panel__field" htmlFor={`${id}-${field}`}>
                  <span>{label}</span>
                  <input
                    id={`${id}-${field}`}
                    ref={field === "sizeMin" ? register("size") : undefined}
                    type="text"
                    inputMode="decimal"
                    autoComplete="off"
                    value={draft[field]}
                    disabled={draft.sizeUnknown && draft[field] === ""}
                    aria-invalid={errors.size ? true : undefined}
                    aria-describedby={describedBy("size")}
                    onChange={(event) => update({[field]: event.target.value})}
                  />
                </label>
                <label className="filter-panel__field" htmlFor={`${id}-${unitField}`}>
                  <span>{label} unit</span>
                  <select
                    id={`${id}-${unitField}`}
                    value={draft[unitField]}
                    onChange={(event) => update({[unitField]: event.target.value as SizeUnit})}
                  >
                    {SIZE_UNITS.map((unit) => <option key={unit.value} value={unit.value}>{unit.label}</option>)}
                  </select>
                </label>
              </div>
            ))}
          <label className="filter-panel__choice">
            <input
              type="checkbox"
              checked={draft.sizeUnknown}
              onChange={(event) => update({sizeUnknown: event.target.checked})}
            />
            <span>Only entries with unknown size</span>
          </label>
          {errorText("size")}
        </fieldset>

        <fieldset className="filter-panel__group" aria-describedby={describedBy("modified", `${id}-date-hint`)}>
          <legend>File modified date</legend>
          <p id={`${id}-date-hint`} className="filter-panel__hint">
            Uses the filesystem modification time, not photo capture time. Days start at local midnight in
            your time zone: <strong>{timeZone}</strong>.
          </p>
          <label className="filter-panel__field" htmlFor={`${id}-from`}>
            <span>Modified on or after</span>
            <input
              id={`${id}-from`}
              ref={register("modified")}
              type="date"
              value={draft.modifiedFrom}
              aria-invalid={errors.modified ? true : undefined}
              aria-describedby={describedBy("modified")}
              onChange={(event) => update({modifiedFrom: event.target.value})}
            />
          </label>
          <label className="filter-panel__field" htmlFor={`${id}-to`}>
            <span>Modified on or before</span>
            <input
              id={`${id}-to`}
              type="date"
              value={draft.modifiedTo}
              aria-invalid={errors.modified ? true : undefined}
              aria-describedby={describedBy("modified")}
              onChange={(event) => update({modifiedTo: event.target.value})}
            />
          </label>
          <label className="filter-panel__choice">
            <input
              type="checkbox"
              checked={draft.modifiedUnknown}
              onChange={(event) => update({modifiedUnknown: event.target.checked})}
            />
            <span>Only entries with unknown modified time</span>
          </label>
          {errorText("modified")}
        </fieldset>

        <footer className="filter-panel__actions">
          <button
            className="file-page-control interactive"
            type="button"
            onClick={() => {
              setDraft(emptyFilterDraft());
              setErrors({});
            }}
          >
            Clear all
          </button>
          <button className="file-page-control interactive" type="button" onClick={onCancel}>Cancel</button>
          <button className="file-page-control file-page-control--primary interactive" type="submit">
            Apply filters
          </button>
        </footer>
      </form>
    </dialog>
  );
}
