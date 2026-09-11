import {useQueryClient} from "@tanstack/react-query";
import {useState} from "react";
import {useNavigate} from "react-router";
import {logoutSession} from "./api";
import {purgePrivateBrowserState} from "./cache";

export function LogoutButton() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);

  const logout = async () => {
    if (pending) return;
    setPending(true);
    setFailed(false);
    try {
      await logoutSession();
      await purgePrivateBrowserState(queryClient);
      navigate("/login", {replace: true});
    } catch {
      setFailed(true);
      setPending(false);
    }
  };

  return (
    <div className="logout-control">
      <button type="button" onClick={logout} disabled={pending}>
        {pending ? "Signing out…" : "Sign out"}
      </button>
      {failed ? <p role="alert">Sign out could not be completed.</p> : null}
    </div>
  );
}
