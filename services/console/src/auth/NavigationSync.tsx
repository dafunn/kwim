import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { setNavigator } from "./session";

/** Registers the router's navigate function for use outside React (queryClient's 401 handler). */
export function NavigationSync(): null {
  const navigate = useNavigate();

  useEffect(() => {
    setNavigator((to) => navigate(to));
  }, [navigate]);

  return null;
}
