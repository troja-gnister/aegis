import {useEffect} from "react";
import {AppShell} from "../layout/AppShell";

export function App() {
  useEffect(() => {
    document.documentElement.dataset.theme = "dark";
  }, []);

  return (
    <AppShell>
      <h1>Secure file access</h1>
      <p>The authenticated root shell is delivered later in Phase 1.</p>
    </AppShell>
  );
}
