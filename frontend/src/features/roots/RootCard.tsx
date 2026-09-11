import {useState} from "react";
import type {RootShell} from "./types";

type RootCardProps = {
  root: RootShell;
};

function hostCapability(mode: RootShell["mode"]): string {
  return mode === "read_write"
    ? "Host capability: managed writes declared"
    : "Host capability: read only";
}

export function RootCard({root}: RootCardProps) {
  const [open, setOpen] = useState(false);
  const detailsId = `root-${root.id}-details`;

  return (
    <article className="root-card">
      <button
        className="root-card__button interactive"
        type="button"
        aria-expanded={open}
        aria-controls={detailsId}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="root-card__icon" aria-hidden="true">
          ◫
        </span>
        <span className="root-card__body">
          <span className="root-card__title" role="heading" aria-level={2}>
            {root.displayName}
          </span>
          <span className="root-card__access">Original access: read only</span>
          <span className="root-card__capability">{hostCapability(root.mode)}</span>
        </span>
        <span className="root-card__chevron" aria-hidden="true">
          ›
        </span>
      </button>
      {open ? (
        <div className="root-card__details" id={detailsId}>
          <p>File browsing arrives in Phase 2.</p>
          <p>Mounted originals remain read only.</p>
        </div>
      ) : null}
    </article>
  );
}
