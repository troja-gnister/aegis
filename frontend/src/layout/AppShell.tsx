import type {ReactNode} from "react";
import {Link} from "react-router";
import {LogoutButton} from "../features/auth/LogoutButton";

type AppShellProps = {
  children: ReactNode;
  username: string;
};

export function AppShell({children, username}: AppShellProps) {
  return (
    <div className="app-shell">
      <header className="app-shell__header">
        <Link className="app-shell__brand interactive" to="/roots" aria-label="Aegis home">
          Aegis
        </Link>
        <div className="app-shell__account">
          <span className="app-shell__user" title={username}>
            {username}
          </span>
          <LogoutButton />
        </div>
      </header>
      <main className="app-shell__main">{children}</main>
    </div>
  );
}
