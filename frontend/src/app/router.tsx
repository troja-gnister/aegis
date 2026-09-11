import {BrowserRouter, Navigate, Route, Routes} from "react-router";
import {AuthBoundary} from "../features/auth/AuthBoundary";
import {LoginPage} from "../features/auth/LoginPage";
import {RootListPage} from "../features/roots/RootListPage";
import {App} from "./App";

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/roots"
        element={
          <AuthBoundary>
            <App>
              <RootListPage />
            </App>
          </AuthBoundary>
        }
      />
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
