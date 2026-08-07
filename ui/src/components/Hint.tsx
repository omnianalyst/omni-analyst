import { define } from "../lib/glossary";

// A term that explains itself on hover and on keyboard focus.
//
// The product cannot avoid precise vocabulary -- "RS percentile" and "calibrated
// threshold" mean something exact -- but a reader meeting one for the first time
// needs a way in that does not cost precision. The definition rides on the term
// itself rather than living in a separate glossary page nobody opens.
//
// Focusable and described by the tooltip, so a keyboard or screen-reader user
// reaches the same explanation a mouse user does. Native `title` is deliberately
// not used: it never appears on focus, and its delay is long enough that most
// readers never see it.

export function Hint({
  term,
  children,
  text,
}: {
  term?: string;
  children: preact.ComponentChildren;
  text?: string;
}) {
  const body = text ?? (term ? define(term) : undefined);
  if (!body) return <>{children}</>;
  return (
    <span class="hint" tabIndex={0} role="note" aria-label={body}>
      {children}
      <span class="hint-bubble" role="tooltip">
        {body}
      </span>
    </span>
  );
}
