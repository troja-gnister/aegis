import {useEffect, useMemo, useRef, useState} from "react";
import type {MediaDetails} from "../types";
import {useViewingDerivative} from "./useViewingDerivative";
import {browserCanPlayType, chooseVideoSource, type CanPlayType} from "./video-source";

export type VideoStageProps = {
  entryId: string;
  details: MediaDetails | undefined;
  detailsFailed: boolean;
  refetchDetails(): Promise<unknown>;
  /** The summary's thumbnail, used as the poster until the poster derivative is known. */
  thumbnailUrl: string | null;
  /** Accessible name for the video element. */
  title: string;
  /** For tests; the browser's own answer by default. */
  canPlayType?: CanPlayType;
};

const PREPARING_TEXT = "Preparing playback…";

/**
 * The video viewer stage. The original plays inline when the browser can
 * play its probed codecs; otherwise (or when the original fails to play) a
 * transcode is requested once and polled until ready. Native controls, the
 * poster derivative, and a retry on playback errors.
 */
export function VideoStage({
  entryId,
  details,
  detailsFailed,
  refetchDetails,
  thumbnailUrl,
  title,
  canPlayType = browserCanPlayType,
}: VideoStageProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [originalFailed, setOriginalFailed] = useState(false);
  const [playbackFailed, setPlaybackFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const choice = useMemo(
    () => (details ? chooseVideoSource(details, canPlayType) : null),
    [canPlayType, details],
  );
  const wantsTranscode = choice?.source === "transcode" || (choice?.source === "original" && originalFailed);
  const transcode = useViewingDerivative({
    entryId,
    kind: "transcode",
    details,
    detailsFailed,
    refetchDetails,
    enabled: wantsTranscode,
  });
  const source = choice?.source === "original" && !originalFailed
    ? {kind: "original" as const, url: choice.url}
    : transcode.url !== null ? {kind: "transcode" as const, url: transcode.url} : null;
  const poster = details?.derivatives.poster?.url ?? thumbnailUrl ?? undefined;

  useEffect(() => {
    const video = videoRef.current;
    // Stop downloading when the source changes or the viewer leaves this video.
    return () => {
      if (!video) return;
      try {
        video.pause();
        video.removeAttribute("src");
        video.load();
      } catch {
        // Media element support varies; removal from the document stops playback anyway.
      }
    };
  }, [source?.url, attempt]);

  let status: string | null = null;
  let action: {label: string; run(): void; disabled?: boolean} | null = null;
  let download: string | null = null;
  if (choice?.source === "none" || transcode.phase === "unavailable") {
    status = "Your access to this file does not include playing it.";
  } else if (playbackFailed) {
    status = "This video could not be played.";
    action = {
      label: "Retry playback",
      run: () => {
        setPlaybackFailed(false);
        setAttempt((current) => current + 1);
      },
    };
  } else if (wantsTranscode && transcode.phase === "unsupported") {
    // Unsupported or quarantined: never retried; the poster stays and Download remains (with EXPORT).
    status = "Playback isn't available for this file.";
    download = details?.permissions.export ? details.original.downloadUrl : null;
  } else if (wantsTranscode && transcode.phase === "busy") {
    status = "Busy, retrying…";
  } else if (wantsTranscode && transcode.phase === "backoff") {
    status = "Couldn't prepare this file; try again later.";
    action = {label: "Retry playback", run: transcode.retry, disabled: !transcode.retryReady};
  } else if (wantsTranscode && transcode.phase === "failed") {
    status = "Playback could not be prepared.";
    action = {label: "Retry playback", run: transcode.retry};
  } else if (wantsTranscode && transcode.phase === "stalled") {
    status = "Preparing playback is taking longer than expected.";
    action = {label: "Retry playback", run: transcode.retry};
  } else if (!details && !detailsFailed) {
    status = "Loading video…";
  } else if (wantsTranscode && source === null && (transcode.phase === "preparing" || transcode.phase === "loading")) {
    status = PREPARING_TEXT;
  }

  return (
    <div className="video-stage" data-testid="video-stage" data-source={source?.kind}>
      {source !== null && !playbackFailed ? (
        <video
          key={`${source.url}#${attempt}`}
          ref={videoRef}
          className="video-stage__video"
          src={source.url}
          poster={poster}
          controls
          playsInline
          preload="metadata"
          aria-label={title}
          onError={() => {
            if (source.kind === "original") setOriginalFailed(true);
            else setPlaybackFailed(true);
          }}
        />
      ) : poster !== undefined ? (
        <img className="video-stage__poster" src={poster} alt="" decoding="async" draggable={false} />
      ) : null}
      {status !== null ? (
        <div className="viewer-stage__status">
          <p role="status">{status}</p>
          {action !== null ? (
            <button className="viewer-button interactive" type="button" disabled={action.disabled} onClick={action.run}>
              {action.label}
            </button>
          ) : null}
          {download !== null ? (
            <a className="viewer-button interactive" href={download} download>Download original</a>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
