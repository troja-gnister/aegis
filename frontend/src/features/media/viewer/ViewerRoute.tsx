import {useCallback} from "react";
import {useLocation, useNavigate, useParams} from "react-router";
import {UUID} from "../../../api/validate";
import {useAuthSession} from "../../auth/session";
import {FileNavigationRouteConsumer} from "../../files/FilesPage";
import type {FileNavigation} from "../../files/navigation";
import {useAuthorizedRoot} from "../../roots/useAuthorizedRoot";
import type {MediaViewerContext} from "../types";
import {filesPath, parseMediaViewerState, viewerLocationState, viewerPath} from "../viewer-route";
import {Viewer, ViewerMessage} from "./Viewer";
import {
  STANDALONE_VIEWER_LIST,
  useDirectoryViewerList,
  useTimelineViewerList,
} from "./viewer-list";

type ContextViewerProps = {
  rootId: string;
  rootEpoch: number;
  entryId: string;
  namespace: string;
  onNavigate(entryId: string): void;
  onClose(): void;
};

function DirectoryViewer({context, navigation, ...props}: ContextViewerProps & {
  context: Extract<MediaViewerContext, {source: "directory"}>;
  navigation: FileNavigation | null;
}) {
  const list = useDirectoryViewerList(context, props.rootEpoch, props.namespace, navigation, props.entryId);
  return <Viewer rootId={props.rootId} entryId={props.entryId} list={list} onNavigate={props.onNavigate} onClose={props.onClose} />;
}

function TimelineViewer({context, ...props}: ContextViewerProps & {
  context: Extract<MediaViewerContext, {source: "timeline"}>;
}) {
  const list = useTimelineViewerList(context, props.rootEpoch, props.namespace, props.entryId);
  return <Viewer rootId={props.rootId} entryId={props.entryId} list={list} onNavigate={props.onNavigate} onClose={props.onClose} />;
}

function ViewerPage({navigation}: {navigation: FileNavigation | null}) {
  const {rootId = "", entryId = ""} = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const {session} = useAuthSession();
  const namespace = session.cacheNamespace;
  const validIds = UUID.test(rootId) && UUID.test(entryId);
  const state = parseMediaViewerState(location.state);
  // History state is trusted only for this exact route: IDs, the timeline filter and the opener's key.
  const context = state !== null && state.aegisMediaViewer.entryId === entryId &&
    state.aegisMediaViewer.context.rootId === rootId
    ? state.aegisMediaViewer.context
    : null;
  const {rootQuery, root, sessionExpiring} = useAuthorizedRoot(rootId, validIds);

  const close = useCallback(() => {
    // The opener is the previous history entry: moving between items replaces the viewer's entry.
    if (context !== null) navigate(-1);
    else navigate(validIds ? filesPath(rootId) : "/roots", {replace: true});
  }, [context, navigate, rootId, validIds]);

  const showEntry = useCallback((nextId: string) => {
    navigate(viewerPath(rootId, nextId), {
      replace: true,
      state: context === null ? null : viewerLocationState(nextId, context),
    });
  }, [context, navigate, rootId]);

  if (!validIds) return <ViewerMessage message="This file is not available in the viewer." onClose={close} />;
  if (rootQuery.isPending || sessionExpiring) return <ViewerMessage message="Loading…" onClose={close} loading />;
  if (rootQuery.isError) return <ViewerMessage message="The authorized root could not be loaded." onClose={close} />;
  if (!root) return <ViewerMessage message="This root is not assigned to this account." onClose={close} />;

  const props: ContextViewerProps = {
    rootId,
    rootEpoch: root.authorizationEpoch,
    entryId,
    namespace,
    onNavigate: showEntry,
    onClose: close,
  };
  if (context?.source === "directory") {
    return <DirectoryViewer key={context.returnKey} context={context} navigation={navigation} {...props} />;
  }
  if (context?.source === "timeline") {
    return <TimelineViewer key={context.returnKey} context={context} {...props} />;
  }
  return <Viewer rootId={rootId} entryId={entryId} list={STANDALONE_VIEWER_LIST} onNavigate={showEntry} onClose={close} />;
}

/**
 * `/files/:rootId/view/:entryId`. The history state names the context the
 * viewer moves through; without one (a pasted link), it shows just the entry.
 */
export function ViewerRoute() {
  return (
    <FileNavigationRouteConsumer>
      {({navigation}) => <ViewerPage navigation={navigation} />}
    </FileNavigationRouteConsumer>
  );
}
