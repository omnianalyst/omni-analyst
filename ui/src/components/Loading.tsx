export function Loading({ label }: { label?: string }) {
  // One loading identity everywhere: the centered wordmark-and-ring the boot
  // screen paints, reused for every in-app wait. It renders inside `main`,
  // so the header stays on screen -- the thing the SPA work was for.
  return (
    <div class="loading" role="status" aria-label={label ?? "Loading"}>
      <div class="loading-brand">
        <span class="loading-word">OMNI ANALYST</span>
        <span class="spinner" aria-hidden="true" />
        {label ? <span class="loading-note">{label}</span> : null}
      </div>
    </div>
  );
}
