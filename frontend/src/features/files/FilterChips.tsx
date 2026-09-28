import {activeFilterFields, describeFilterField, removeFilter} from "./filter-state";
import type {FileFilters} from "./types";

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
