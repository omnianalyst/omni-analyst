import { LoginView } from "../components/LoginView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Sign in | Omni Analyst" };
}

export default function LoginPage() {
  return <LoginView />;
}
