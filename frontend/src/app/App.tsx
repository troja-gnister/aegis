import {useEffect, type PropsWithChildren} from "react";
import {useAuthSession} from "../features/auth/session";
import {AppShell} from "../layout/AppShell";

export function App({children}: PropsWithChildren) {
  const {session} = useAuthSession();
  useEffect(() => {
    document.documentElement.dataset.theme = "dark";
  }, []);

  return <AppShell username={session.user.username}>{children}</AppShell>;
}
