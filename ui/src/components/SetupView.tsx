import { useState } from "preact/hooks";
import { useNavigate } from "@neutron-build/core/client";
import { ApiHttpError, describeError } from "../lib/api";
import { setAuthToken, setup } from "../lib/auth";

// First-run operator provisioning. The backend /auth/setup endpoint refuses
// once any user exists, so this view is only reachable when the deployment is
// unclaimed. It creates the operator account and stores a token in one step.
export function SetupView() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: Event) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    if (password.length < 12) {
      setError("Password must be at least 12 characters.");
      return;
    }
    setBusy(true);
    try {
      const res = await setup(email, password);
      setAuthToken(res.token);
      navigate("/");
    } catch (err) {
      if (err instanceof ApiHttpError && err.status === 409) {
        // Someone completed setup in another tab. Hand off to sign-in.
        navigate("/login");
        return;
      }
      setError(describeError(err).detail || describeError(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="login-view">
      <header class="page-head">
        <h1>Create the operator account</h1>
        <p class="muted">
          First-run setup. This account owns the autonomous demand attribution
          and BYO credentials, and it is the only one created here -- after this,
          setup is disabled. Choose credentials you control.
        </p>
      </header>

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
              autocomplete="new-password"
              value={password}
              onInput={(e) => setPassword((e.target as HTMLInputElement).value)}
            />
          </label>
          <label class="auth-field">
            <span class="auth-label">Confirm password</span>
            <input
              class="search-input"
              type="password"
              required
              autocomplete="new-password"
              value={confirm}
              onInput={(e) => setConfirm((e.target as HTMLInputElement).value)}
            />
          </label>
          {error ? <p class="auth-error">{error}</p> : null}
          <button class="search-btn" type="submit" disabled={busy}>
            {busy ? "..." : "Create account"}
          </button>
        </form>
      </section>
    </div>
  );
}
