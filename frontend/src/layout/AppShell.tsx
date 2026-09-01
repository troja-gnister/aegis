import type {ReactNode} from "react";

type AppShellProps = {
  children: ReactNode;
};

export function AppShell({children}: AppShellProps) {
  return (
    <div className="app-shell">
      <header className="app-shell__header">
        <a className="app-shell__brand" href="/" aria-label="Aegis home">
          Aegis
        </a>
      </header>
      <main className="app-shell__main">{children}</main>
    </div>
  );
}
