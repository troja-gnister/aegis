import {StrictMode} from "react";
import {createRoot} from "react-dom/client";
import {AppProviders} from "./app/providers";
import {AppRouter} from "./app/router";
import "./styles/global.css";

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error("The application root is missing.");
}

createRoot(rootElement).render(
  <StrictMode>
    <AppProviders>
      <AppRouter />
    </AppProviders>
  </StrictMode>,
);
