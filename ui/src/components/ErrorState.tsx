export function ErrorState({
  message,
  detail,
}: {
  message: string;
  detail?: string;
}) {
  return (
    <div class="error-state" role="alert">
      <svg
        class="error-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.5"
        aria-hidden="true"
      >
        <path
          stroke-linecap="round"
          stroke-linejoin="round"
          d="M12 9v3.75m0 3.75h.008M5.408 19.5h13.184a1.8 1.8 0 0 0 1.582-2.664l-6.592-11.4a1.8 1.8 0 0 0-3.164 0L3.826 16.836A1.8 1.8 0 0 0 5.408 19.5Z"
        />
      </svg>
      <p class="error-title">{message}</p>
      {detail ? <p class="error-detail">{detail}</p> : null}
    </div>
  );
}
