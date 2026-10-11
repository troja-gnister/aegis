import {Link} from "react-router";
import {filesPath, photosPath} from "./viewer-route";
import "./photos.css";

export type RootSection = "files" | "photos";

/** Navigation between a root's Files and Photos views. */
export function RootSectionNav({rootId, current}: {rootId: string; current: RootSection}) {
  return (
    <nav className="root-sections" aria-label="Root views">
      <Link
        className="root-sections__link interactive"
        to={filesPath(rootId)}
        aria-current={current === "files" ? "page" : undefined}
      >
        Files
      </Link>
      <Link
        className="root-sections__link interactive"
        to={photosPath(rootId)}
        aria-current={current === "photos" ? "page" : undefined}
      >
        Photos
      </Link>
    </nav>
  );
}
