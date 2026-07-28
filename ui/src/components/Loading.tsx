export function Loading({ label }: { label?: string }) {
  return (
    <div class="loading">
      <span class="spinner" aria-hidden="true" />
      <span>{label ?? "Loading\u2026"}</span>
    </div>
  );
}
