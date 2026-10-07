import { type FormEvent, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { getHealth } from "../api/health";
import { login } from "../auth/session";
import styles from "./Login.module.css";

type Notice = { kind: "error" | "warning"; text: string } | null;

/** Failed logins carry no distinguishing detail; only 429 (lockout) and 503 (console disabled or not provisioned) get their own state. */
function noticeFor(error: unknown): Notice {
  if (!(error instanceof ApiError)) {
    return { kind: "error", text: "Could not reach the server. Try again." };
  }
  if (error.status === 429) {
    return { kind: "warning", text: error.detail };
  }
  if (error.status === 503) {
    return { kind: "warning", text: error.detail };
  }
  return { kind: "error", text: error.detail };
}

export function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const navigate = useNavigate();
  const health = useQuery({ queryKey: ["health"], queryFn: getHealth, retry: false });

  const mutation = useMutation({
    mutationFn: login,
    onSuccess: () => navigate("/teams", { replace: true }),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate({ username, password });
  }

  const notice = mutation.isError ? noticeFor(mutation.error) : null;

  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <h1 className={styles.title}>KWIM Admin</h1>
        {notice && (
          <p className={`${styles.message} ${styles[notice.kind]}`} role="alert">
            {notice.text}
          </p>
        )}
        <form onSubmit={handleSubmit}>
          <div className={styles.field}>
            <label htmlFor="username">Username</label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>
          <div className={styles.field}>
            <label htmlFor="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button type="submit" className={styles.submit} disabled={mutation.isPending}>
            {mutation.isPending ? "Logging in..." : "Log in"}
          </button>
        </form>
        {health.data && <p className={styles.version}>{health.data.service} v{health.data.version}</p>}
      </div>
    </div>
  );
}
