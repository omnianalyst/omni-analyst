import { useEffect, useState } from "preact/hooks";
import { useNavigate } from "@neutron-build/core/client";
import { ApiHttpError, describeError } from "../lib/api";
import {
  clearAuthToken,
  getAuthToken,
  login,
  setAuthToken,
} from "../lib/auth";

function authErrorMessage(err: unknown): string {
  if (err instanceof ApiHttpError) {
    // The login endpoint answers a uniform 401 for unknown email / wrong
    // password / inactive, so it cannot be used to enumerate accounts. Surface
    // that same wording rather than a raw status.
    if (err.status === 401) return "Invalid email or password.";
    if (err.status === 400) {
      try {
        const body = JSON.parse(err.body);
        if (body.detail) return String(body.detail);
      } catch {
        /* fall through */
      }
    }
    return describeError(err).detail || describeError(err).message;
  }
  return describeError(err).message;
}

export function LoginView() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [signedIn, setSignedIn] = useState(false);

  useEffect(() => {
    setSignedIn(getAuthToken() !== null);
  }, []);

  async function onSubmit(e: Event) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const res = await login(email, password);
      setAuthToken(res.token);
      setSignedIn(true);
      navigate("/");
    } catch (err) {
      setError(authErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  function signOut() {
    clearAuthToken();
    setSignedIn(false);
    setEmail("");
    setPassword("");
  }

  return (
    <div class="login-view">
      <header class="page-head">
        <h1>{signedIn ? "Signed in" : "Sign in"}</h1>
        <p class="muted">
          A watchlist and your BYO credentials are private to your account.
          Identity is a verified token, never a header a caller can name.
        </p>
      </header>

      {signedIn ? (
        <section class="panel">
          <div style={{ padding: "18px" }}>
            <p>You are signed in. Private coverage and watchlists are now reachable.</p>
            <div style={{ marginTop: "14px", display: "flex", gap: "12px" }}>
              <a class="search-btn" href="/watchlist" style={{ textDecoration: "none" }}>
                Watchlists
              </a>
              <button class="search-btn" type="button" onClick={signOut}>
                Sign out
              </button>
            </div>
          </div>
        </section>
      ) : (
        <section class="panel">
          <form class="auth-form" onSubmit={onSubmit}>
            <label class="auth-field">
              <span class="auth-label">Email</span>
              <input
                class="search-input"
                type="email"
                required
                autocomplete="email"
                value={email}
                onInput={(e) => setEmail((e.target as HTMLInputElement).value)}
              />
            </label>
            <label class="auth-field">
              <span class="auth-label">Password</span>
              <input
                class="search-input"
                type="password"
                required
                minlength={8}
                autocomplete="current-password"
                value={password}
                onInput={(e) => setPassword((e.target as HTMLInputElement).value)}
              />
            </label>
            {error ? <p class="auth-error">{error}</p> : null}
            <button class="search-btn" type="submit" disabled={busy}>
              {busy ? "..." : "Sign in"}
            </button>
          </form>
        </section>
      )}
    </div>
  );
}
