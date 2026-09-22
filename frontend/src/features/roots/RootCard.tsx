import {Link} from "react-router";
import type {RootShell} from "./types";

type RootCardProps = {
  root: RootShell;
};

function hostCapability(mode: RootShell["mode"]): string {
  return mode === "read_write"
    ? "Legacy host declaration: read/write"
    : "Host capability: read only";
}

export function RootCard({root}: RootCardProps) {
  return (
    <article className="root-card">
      <Link
        className="root-card__button interactive"
        to={`/files/${root.id}`}
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
      </Link>
    </article>
  );
}
