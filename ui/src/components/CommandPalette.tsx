import { useEffect, useRef, useState } from "preact/hooks";
import { navigate } from "@neutron-build/core/client";
import { filterRoutes, type CommandItem } from "../lib/command";

export const OPEN_COMMAND_PALETTE = "omni:open-command-palette";

function isTyping(): boolean {
  const el = document.activeElement as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || el.isContentEditable;
}

export function CommandPalette({ commands }: { commands: CommandItem[] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const filtered = filterRoutes(commands, query);

  // Reset selection whenever the result set or open state changes, so the
  // highlight never points past the end of the list.
  useEffect(() => {
    setActive(0);
  }, [query, open]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(
      `li[data-idx="${active}"]`,
    );
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  useEffect(() => {
    function go(href: string) {
      // navigate() takes the typed route union; the palette deals in plain
      // hrefs, so cast rather than constrain every caller to the route map.
      navigate(href as never);
      setOpen(false);
      setQuery("");
    }

    function onKey(e: KeyboardEvent) {
      // Cmd/Ctrl+K toggles from anywhere, including while typing.
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
        return;
      }
      if (open) {
        if (e.key === "Escape") {
          e.preventDefault();
          setOpen(false);
          return;
        }
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setActive((a) => Math.min(a + 1, Math.max(filtered.length - 1, 0)));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setActive((a) => Math.max(a - 1, 0));
          return;
        }
        if (e.key === "Enter") {
          const item = filtered[active];
          if (item) {
            e.preventDefault();
            go(item.href);
          }
          return;
        }
        return;
      }
      // Digit hotkeys 1-9 jump to that route, but never while the user is
      // typing into a field (so a "1" in search does not yank them away).
      if (!isTyping() && /^[1-9]$/.test(e.key)) {
        const item = commands[Number(e.key) - 1];
        if (item) {
          e.preventDefault();
          go(item.href);
        }
      }
    }

    function onOpen() {
      setOpen(true);
    }

    window.addEventListener("keydown", onKey);
    window.addEventListener(OPEN_COMMAND_PALETTE, onOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(OPEN_COMMAND_PALETTE, onOpen);
    };
  }, [open, filtered, active, commands]);

  if (!open) return null;

  return (
    <div class="palette-backdrop" onClick={() => setOpen(false)}>
      <div class="palette" onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          class="palette-input"
          placeholder="Jump to a destination"
          value={query}
          onInput={(e) =>
            setQuery((e.currentTarget as HTMLInputElement).value)
          }
        />
        {filtered.length === 0 ? (
          <p class="palette-empty">No matching destination.</p>
        ) : (
          <ul class="palette-list" ref={listRef}>
            {filtered.map((item, i) => (
              <li key={item.href} data-idx={i}>
                <a
                  href={item.href}
                  class={`palette-row ${i === active ? "palette-row-active" : ""}`}
                  onMouseEnter={() => setActive(i)}
                  onClick={(e) => {
                    e.preventDefault();
                    navigate(item.href as never);
                    setOpen(false);
                    setQuery("");
                  }}
                >
                  <span class="palette-label">{item.label}</span>
                  {item.hint ? <kbd class="palette-hint">{item.hint}</kbd> : null}
                </a>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
