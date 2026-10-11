import {BrowserRouter, Navigate, Outlet, Route, Routes} from "react-router";
import {AuthBoundary, PrivateContentGate} from "../features/auth/AuthBoundary";
import {LoginPage} from "../features/auth/LoginPage";
import {FileNavigationProvider, FilesPage} from "../features/files/FilesPage";
import {PhotosPage} from "../features/media/PhotosPage";
import {ViewerRoute} from "../features/media/viewer/ViewerRoute";
import {RootListPage} from "../features/roots/RootListPage";
import {App} from "./App";

function AuthenticatedApp() {
  return (
    <AuthBoundary>
      {({checking}) => (
        <FileNavigationProvider>
          <PrivateContentGate checking={checking}>
            <App><Outlet /></App>
          </PrivateContentGate>
        </FileNavigationProvider>
      )}
    </AuthBoundary>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<AuthenticatedApp />}>
        <Route path="/roots" element={<RootListPage />} />
        <Route path="/files/:rootId" element={<FilesPage />} />
        <Route path="/files/:rootId/directories/:parentId" element={<FilesPage />} />
        <Route path="/files/:rootId/photos" element={<PhotosPage />} />
        <Route path="/files/:rootId/view/:entryId" element={<ViewerRoute />} />
      </Route>
      <Route path="/" element={<Navigate to="/roots" replace />} />
      <Route path="*" element={<Navigate to="/roots" replace />} />
    </Routes>
  );
}

export function AppRouter() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  );
}
