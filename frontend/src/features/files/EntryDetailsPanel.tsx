import {useQuery, useQueryClient} from "@tanstack/react-query";
import {useEffect, useRef} from "react";
import {ApiProblem} from "../../api/problem";
import {useAuthSession} from "../auth/session";
import {approximateBytes, formatExactCount, formatLocalDateTime, nanosecondsToInstant} from "./format";
import {entryQueryOptions} from "./queries";
import type {EntryDetails, EntryKind, SourceState} from "./types";

export type EntryDetailsPanelProps = {
  entryId: string;
  onClose(): void;
};

const KIND_TEXT: Record<EntryKind, string> = {
  directory: "Directory",
  file: "File",
  symlink: "Symbolic link",
  special: "Special filesystem entry",
};

const SOURCE_TEXT: Record<SourceState, string> = {
  present: "Present when last indexed",
  missing: "Missing when last indexed",
  inaccessible: "Currently inaccessible",
  unsupported: "Unsupported entry",
};

function informationalNotes(entry: EntryDetails): string[] {
  const notes: string[] = [];
  if (entry.kind === "symlink") {
    notes.push("This is a symbolic link. Links are recorded as metadata and never followed.");
  }
  if (entry.kind === "special") {
    notes.push("This special filesystem entry is recorded as metadata only and is never opened.");
  }
  if (entry.sourceState === "unsupported") {
    notes.push("This entry type is not supported for content access.");
  } else if (entry.sourceState === "inaccessible") {
    notes.push("This entry or one of its folders is currently inaccessible. Metadata is from the last index.");
  } else if (entry.sourceState === "missing") {
    notes.push("This entry was missing when last indexed. Its last indexed metadata is kept.");
  }
  return notes;
}

function SizeValue({size}: {size: string | null}) {
  if (size === null) return <>Size unknown</>;
  const approximate = approximateBytes(size);
  return <>{formatExactCount(size)} bytes{approximate ? ` (about ${approximate})` : ""}</>;
}

function ModifiedValue({modifiedNs}: {modifiedNs: string | null}) {
  const instant = modifiedNs === null ? null : nanosecondsToInstant(modifiedNs);
  if (!instant) return <>Modified time unknown</>;
  return (
    <>
      <time dateTime={instant.utc}>{formatLocalDateTime(instant.date)}</time>
      <small className="entry-details__exact">Exact: {instant.utc}</small>
    </>
  );
}

function detailsError(error: unknown): string {
  if (error instanceof ApiProblem && error.status === 404) {
    return "This entry is no longer in the index, or it is no longer available to you.";
  }
  return "File details could not be loaded.";
}

export function EntryDetailsPanel({entryId, onClose}: EntryDetailsPanelProps) {
  const {session} = useAuthSession();
  const queryClient = useQueryClient();
  const options = entryQueryOptions(session.cacheNamespace, entryId);
  const query = useQuery({...options, refetchOnWindowFocus: false});
  const headingRef = useRef<HTMLHeadingElement>(null);
  const queryKey = options.queryKey;
  const keyText = JSON.stringify(queryKey);

  useEffect(() => {
    headingRef.current?.focus({preventScroll: true});
  }, [entryId]);

  useEffect(() => {
    const key = JSON.parse(keyText) as unknown[];
    // Closing, selecting another entry or navigating cancels this details request.
    return () => {
      void queryClient.cancelQueries({queryKey: key, exact: true});
    };
  }, [keyText, queryClient]);

  // Placeholder data is never shown: a different entry ID is a different query.
  const entry = query.data?.id === entryId ? query.data : undefined;
  const notes = entry ? informationalNotes(entry) : [];
  const typeText = entry ? (entry.typeHint === null ? "No extension" : `.${entry.typeHint}`) : "";

  return (
    <section className="entry-details" aria-label="File details">
      <div className="entry-details__header">
        <div className="entry-details__heading">
          <p className="eyebrow">Details</p>
          <h2 ref={headingRef} tabIndex={-1} dir="auto">
            {entry?.displayName ?? "Selected entry"}
          </h2>
        </div>
        <button className="file-page-control interactive" type="button" onClick={onClose}>Close details</button>
      </div>
      {query.isPending ? <p role="status">Loading file details…</p> : null}
      {query.isError ? (
        <div className="entry-details__error">
          <p className="notice notice--error" role="alert">{detailsError(query.error)}</p>
          {!(query.error instanceof ApiProblem && query.error.status === 404) ? (
            <button className="file-page-control interactive" type="button" onClick={() => void query.refetch()}>
              Retry details
            </button>
          ) : null}
        </div>
      ) : null}
      {entry ? (
        <>
          <dl className="entry-details__list">
            <div><dt>Kind</dt><dd>{KIND_TEXT[entry.kind]}</dd></div>
            <div><dt>Type</dt><dd>{typeText}</dd></div>
            <div><dt>Size</dt><dd><SizeValue size={entry.size} /></dd></div>
            <div>
              <dt>File modified</dt>
              <dd>
                <ModifiedValue modifiedNs={entry.modifiedNs} />
                <small>Filesystem modification time, not a capture time.</small>
              </dd>
            </div>
            <div><dt>Availability</dt><dd>{SOURCE_TEXT[entry.sourceState]}</dd></div>
            <div><dt>Catalog version</dt><dd>{formatExactCount(entry.version)}</dd></div>
          </dl>
          {notes.map((note) => <p key={note} className="notice">{note}</p>)}
          <ol className="entry-details__location" aria-label="Location">
            {entry.ancestorsTruncated ? <li>… earlier folders omitted</li> : null}
            {entry.ancestors.map((ancestor) => (
              <li key={ancestor.id} dir="auto">{ancestor.displayName === "" ? "Root" : ancestor.displayName}</li>
            ))}
          </ol>
          <p className="entry-details__read-only">
            Originals remain read only. Opening, preview and download are not available in this version.
          </p>
        </>
      ) : null}
    </section>
  );
}
