import { useEffect, useRef, useState } from "preact/hooks";
import { navigate } from "@neutron-build/core/client";
import { filterRoutes, type CommandItem } from "../lib/command";
import { request, authHeaderIfPresent } from "../lib/api";

export const OPEN_COMMAND_PALETTE = "omni:open-command-palette";

function isTyping(): boolean {
  const el = document.activeElement as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || el.isContentEditable;
}

interface EntityResult {
  id: string;
  symbol: string | null;
  name: string | null;
  kind: string;
}

// Search-only by design (2026-08-22): navigation lives in the topbar, and
// the palette duplicating it read as a second nav. `commands` stays in the
// signature (optional, empty by default) so callers don't break the day an
// action -- add position, create alert -- belongs here; routes never did.
export function CommandPalette({ commands = [] }: { commands?: CommandItem[] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [entities, setEntities] = useState<EntityResult[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const openRef = useRef(open);
  const activeRef = useRef(active);
  const resultsRef = useRef<Array<{ href: string }>>([]);
  const commandsRef = useRef(commands);

  const navResults = filterRoutes(commands, query);

  useEffect(() => {
    activeRef.current = 0;
    setActive(0);
  }, [query, open]);

  useEffect(() => {
    if (!open) return;
    if (query.trim().length < 2) { setEntities([]); return; }
    const timer = setTimeout(async () => {
      try {
        const resp = await request<{ entities: EntityResult[] }>(
          `/entities?q=${encodeURIComponent(query.trim())}`,
          authHeaderIfPresent(),
        );
        setEntities((resp.entities || []).slice(0, 10));
      } catch { setEntities([]); }
    }, 250);
    return () => clearTimeout(timer);
  }, [query, open]);

  useEffect(() => { if (open) inputRef.current?.focus(); }, [open]);
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`li[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const allResults = [
    ...navResults.map(r => ({ type: "nav" as const, ...r })),
    ...entities.map(e => ({
      type: "entity" as const,
      href: `/entity/${e.id}`,
      label: e.symbol || e.name || e.id,
      sub: e.name || "",
      hint: e.kind,
    })),
  ];
  openRef.current = open;
  activeRef.current = active;
  resultsRef.current = allResults;
  commandsRef.current = commands;

  useEffect(() => {
    function go(href: string) {
      navigate(href as never);
      openRef.current = false;
      setOpen(false);
      setQuery("");
    }
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openRef.current = !openRef.current;
        setOpen(openRef.current);
        return;
      }
      if (openRef.current) {
        if (e.key === "Escape") {
          e.preventDefault();
          openRef.current = false;
          setOpen(false);
          return;
        }
        if (e.key === "ArrowDown") {
          e.preventDefault();
          activeRef.current = Math.min(
            activeRef.current + 1,
            Math.max(resultsRef.current.length - 1, 0),
          );
          setActive(activeRef.current);
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          activeRef.current = Math.max(activeRef.current - 1, 0);
          setActive(activeRef.current);
          return;
        }
        if (e.key === "Enter") {
          const item = resultsRef.current[activeRef.current];
          if (item) { e.preventDefault(); go(item.href); }
          return;
        }
      }
      if (!isTyping() && /^[1-9]$/.test(e.key)) {
        const item = commandsRef.current[Number(e.key) - 1];
        if (item) { e.preventDefault(); go(item.href); }
      }
    }
    function onOpen() {
      openRef.current = true;
      setOpen(true);
    }
    window.addEventListener("keydown", onKey);
    window.addEventListener(OPEN_COMMAND_PALETTE, onOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(OPEN_COMMAND_PALETTE, onOpen);
    };
  }, []);

  if (!open) return null;

  return (
    <div class="palette-backdrop" onClick={() => setOpen(false)}>
      <div class="palette" onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          class="palette-input"
          placeholder="Search a ticker or name..."
          value={query}
          onInput={(e) => setQuery((e.currentTarget as HTMLInputElement).value)}
        />
        {allResults.length === 0 ? (
          <p class="palette-empty">
            {query.trim().length >= 2 ? "Searching..." : "Type a ticker or name to search."}
          </p>
        ) : (
          <ul class="palette-list" ref={listRef}>
            {navResults.length > 0 && query.length > 0 && (
              <li class="palette-section-label">Pages</li>
            )}
            {allResults.map((item, i) => (
              <>
                {"sub" in item && i === navResults.length && (
                  <li class="palette-section-label">Entities</li>
                )}
                <li key={item.href} data-idx={i}>
                  <a
                    href={item.href}
                    class={`palette-row ${i === active ? "palette-row-active" : ""}`}
                    onMouseEnter={() => {
                      activeRef.current = i;
                      setActive(i);
                    }}
                    onClick={(e) => {
                      e.preventDefault();
                      navigate(item.href as never);
                      setOpen(false);
                      setQuery("");
                    }}
                  >
                    <span class="palette-label">{item.label}</span>
                    {"sub" in item && item.sub ? (
                      <span class="palette-sub">{item.sub}</span>
                    ) : null}
                    {item.hint ? <kbd class="palette-hint">{item.hint}</kbd> : null}
                  </a>
                </li>
              </>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
