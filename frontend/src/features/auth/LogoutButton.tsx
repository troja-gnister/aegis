import {useQueryClient} from "@tanstack/react-query";
import {useState} from "react";
import {useNavigate} from "react-router";
import {logoutSession} from "./api";
import {purgePrivateBrowserState} from "./cache";
import {beginSignOut, completeSignOut} from "./session";

export function LogoutButton() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [pending, setPending] = useState(false);

  const logout = async () => {
    if (pending) return;
    setPending(true);
    const generation = beginSignOut();
    // Start with this session's CSRF authority, then immediately remove private
    // content. A server error may arrive after revocation has already happened.
    const request = logoutSession().then(() => true, () => false);
    const cleanup = purgePrivateBrowserState(queryClient).then(() => true, () => false);
    navigate("/login", {replace: true});
    const [confirmed, cleanupSucceeded] = await Promise.all([request, cleanup]);
    completeSignOut(generation, confirmed, cleanupSucceeded);
  };

  return (
    <div className="logout-control">
      <button type="button" onClick={logout} disabled={pending}>
        {pending ? "Signing out…" : "Sign out"}
      </button>
    </div>
  );
}
