import { useEffect, useRef, useState } from "preact/hooks";

// The one place Discover explains itself. The page shows answers; this shows
// the reasoning -- what the system is, why the structure is what it is, and
// what it honestly cannot claim. Kept out of the page body so the page stays
// an answer stack, not an essay.
export function LearnWhy() {
  const [open, setOpen] = useState(false);
  const card = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onClick = (event: MouseEvent) => {
      if (card.current && !card.current.contains(event.target as Node)) setOpen(false);
    };
    // Lock the page behind the modal: scrolling the background while reading
    // a dialog is disorienting and loses the reading position on close.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  return (
    <>
      <button
        type="button"
        class="learn-why-trigger"
        onClick={() => setOpen(true)}
      >
        Learn why
      </button>
      {open ? (
        <div class="learn-why-overlay" role="dialog" aria-modal="true" aria-label="Why this system">
          <div class="learn-why-card" ref={card}>
            <header>
              <h2>Why this is the last portfolio page you need</h2>
              <button type="button" class="learn-why-close" aria-label="Close" onClick={() => setOpen(false)}>
                ×
              </button>
            </header>
            <div class="learn-why-body">
              <section>
                <h3>Everything measurable, one page</h3>
                <p>
                  This system continuously measures the whole liquid universe it can verify --
                  broad stock funds, sectors, bonds, gold, commodities, major digital assets,
                  and 500 individual companies -- and refuses any data feed it can prove is
                  broken. When an asset's feed fails, it drops from the rankings and appears
                  under Coverage in the page footer, with the evidence. What you see here is
                  not a curated tip list; it is the measured average of everything, funnelled
                  to the few decisions that actually matter.
                </p>
              </section>
              <section>
                <h3>Four regimes, because the future has only four shapes</h3>
                <p>
                  Nobody knows what the market does next, but it can only do one of four things:
                  grow, inflate, deflate, or crash. Each regime has an asset class built for it.
                  Holding one sleeve per regime -- a quarter of your money in each -- means no
                  possible future leaves you unprotected. This is the structure behind Harry
                  Browne's Permanent Portfolio and Ray Dalio's All-Weather, with five decades of
                  evidence and no forecasts required.
                </p>
              </section>
              <section>
                <h3>The picks are policy, not last year's winners</h3>
                <p>
                  Each sleeve holds the asset that defines its regime: the whole stock market
                  (VTI), gold (GLD), long Treasuries (TLT), and T-bills (SGOV). Chasing the
                  top-scoring asset of the last few years is the one behaviour that reliably
                  costs investors, so the scores rank the alternatives -- shown beside every
                  pick -- but never the pick itself.
                </p>
              </section>
              <section>
                <h3>Every number is measured, none is promised</h3>
                <p>
                  Median years, worst falls, and up-year rates are computed from complete
                  calendar years of verified prices. The mix's own history -- what holding
                  these four, rebalanced every January, actually did -- is shown with its
                  window named. Past performance is not a forecast, and nothing here is
                  personalised advice.
                </p>
              </section>
              <section>
                <h3>What is deliberately missing</h3>
                <p>
                  No news feed, no hot takes, no price targets. The deeper system logs every
                  prediction it makes and scores it against what actually happened -- its
                  published hit rate is how it earns the right to speak at all. When the data
                  is not enough to say something, this page says nothing, and tells you why.
                </p>
              </section>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
