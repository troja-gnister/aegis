import {activeFilterFields, describeFilterField, removeFilter, SCOPE_OPTIONS} from "./filter-state";
import type {BrowseScope, FileFilters} from "./types";

export type ScopeToggleProps = {
  scope: BrowseScope;
  onChange(scope: BrowseScope): void;
};

/** Where applied filters search: this folder or every folder of the root. */
export function ScopeToggle({scope, onChange}: ScopeToggleProps) {
  return (
    <div className="scope-toggle" role="group" aria-label="Search in">
      {SCOPE_OPTIONS.map((option) => (
        <button
          key={option.value}
          className="interactive"
          type="button"
          aria-pressed={scope === option.value}
          onClick={() => {
            if (scope !== option.value) onChange(option.value);
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export type FilterChipsProps = {
  value: FileFilters;
  onChange(filters: FileFilters): void;
};

export function FilterChips({value, onChange}: FilterChipsProps) {
  const fields = activeFilterFields(value);
  if (fields.length === 0) return null;
  return (
    <ul className="filter-chips" aria-label="Active filters">
      {fields.map((field) => {
        const label = describeFilterField(value, field);
        return (
          <li key={field}>
            <button
              className="filter-chip interactive"
              type="button"
              aria-label={`Remove filter ${label}`}
              onClick={() => onChange(removeFilter(value, field))}
            >
              <span className="filter-chip__label" dir="auto">{label}</span>
              <span aria-hidden="true">×</span>
            </button>
          </li>
        );
      })}
      <li>
        <button className="filter-chip filter-chip--clear interactive" type="button" onClick={() => onChange({v: 1})}>
          Clear all filters
        </button>
      </li>
    </ul>
  );
}
