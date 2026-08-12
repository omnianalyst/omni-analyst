import { useEffect, useRef, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import {
  addBulletinItem,
  getBulletin,
  removeBulletinItem,
  updateBulletinItem,
  type BulletinInput,
  type BulletinItem,
  type BulletinKind,
} from "../lib/bulletin";

export function HeaderBulletin() {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<BulletinItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [editing, setEditing] = useState<BulletinItem | null>(null);
  const [adding, setAdding] = useState(false);
  const [kind, setKind] = useState<BulletinKind>("note");
  const [title, setTitle] = useState("");
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || loaded) return;
    void getBulletin()
      .then((response) => {
        setItems(response.items);
        setLoaded(true);
      })
      .catch((error) => setMessage(describeError(error).message));
  }, [open, loaded]);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const resetForm = () => {
    setAdding(false);
    setEditing(null);
    setKind("note");
    setTitle("");
    setDetail("");
  };

  const beginAdd = (nextKind: BulletinKind) => {
    resetForm();
    setKind(nextKind);
    setAdding(true);
  };

  const beginEdit = (item: BulletinItem) => {
    setAdding(false);
    setEditing(item);
    setKind(item.kind);
    setTitle(item.title);
    setDetail(item.kind === "link" ? item.url ?? "" : item.body ?? "");
  };

  const save = async (event: Event) => {
    event.preventDefault();
    const input: BulletinInput = {
      kind,
      title,
      body: kind === "note" ? detail : null,
      url: kind === "link" ? detail : null,
    };
    setBusy(true);
    setMessage(null);
    try {
      const saved = editing
        ? await updateBulletinItem(editing.id, input)
        : await addBulletinItem(input);
      setItems((current) => [saved, ...current.filter((item) => item.id !== saved.id)]);
      resetForm();
    } catch (error) {
      setMessage(describeError(error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (item: BulletinItem) => {
    setBusy(true);
    try {
      await removeBulletinItem(item.id);
      setItems((current) => current.filter((entry) => entry.id !== item.id));
      if (editing?.id === item.id) resetForm();
    } catch (error) {
      setMessage(describeError(error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="header-bulletin" ref={root}>
      <button
        type="button"
        class={`bulletin-trigger ${items.length ? "has-items" : ""}`}
        title="Pinned notes and links"
        aria-label="Open pinned notes and links"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
          <path stroke-linecap="round" stroke-linejoin="round" d="M15.5 4.5 19 8l-3 2.2-.7 4.1-2.1 2.1-3.6-3.6-5.1 5.1 4.1-6.1L5 8.2l2.1-2.1 4.1-.7 2.2-3Z" />
        </svg>
        {items.length ? <span class="bulletin-count">{items.length}</span> : null}
      </button>

      {open ? (
        <aside class="bulletin-dropdown" aria-label="Pinned bulletin">
          <header>
            <div><strong>Pinned</strong><span>Private notes and links</span></div>
            <div class="bulletin-add-actions">
              <button type="button" onClick={() => beginAdd("note")}>Note</button>
              <button type="button" onClick={() => beginAdd("link")}>Link</button>
            </div>
          </header>

          {message ? <p class="bulletin-message" role="status">{message}</p> : null}

          {adding || editing ? (
            <form class="bulletin-form" onSubmit={(event) => void save(event)}>
              <div class="bulletin-kind-switch">
                <button type="button" class={kind === "note" ? "active" : ""} onClick={() => { setKind("note"); setDetail(""); }}>Note</button>
                <button type="button" class={kind === "link" ? "active" : ""} onClick={() => { setKind("link"); setDetail(""); }}>Link</button>
              </div>
              <input required maxlength={100} value={title} onInput={(event) => setTitle(event.currentTarget.value)} placeholder={kind === "note" ? "Note title" : "Link name"} />
              {kind === "note" ? (
                <textarea maxlength={1000} rows={3} value={detail} onInput={(event) => setDetail(event.currentTarget.value)} placeholder="A quick reminder…" />
              ) : (
                <input required type="url" value={detail} onInput={(event) => setDetail(event.currentTarget.value)} placeholder="https://…" />
              )}
              <div class="bulletin-form-actions">
                <button type="button" onClick={resetForm}>Cancel</button>
                <button class="bulletin-save" type="submit" disabled={busy}>Save</button>
              </div>
            </form>
          ) : null}

          <div class="bulletin-items">
            {!loaded ? <p class="bulletin-empty">Loading…</p> : items.length === 0 ? (
              <p class="bulletin-empty">Nothing pinned yet.</p>
            ) : items.map((item) => (
              <article class={`bulletin-item bulletin-item-${item.kind}`} key={item.id}>
                <div class="bulletin-item-copy">
                  {item.kind === "link" && item.url ? (
                    <a href={item.url} target="_blank" rel="noopener noreferrer"><strong>{item.title}</strong><span>{new URL(item.url).hostname}</span></a>
                  ) : (
                    <><strong>{item.title}</strong>{item.body ? <p>{item.body}</p> : null}</>
                  )}
                </div>
                <div class="bulletin-item-actions">
                  <button type="button" title="Edit" onClick={() => beginEdit(item)}>Edit</button>
                  <button type="button" title="Remove" disabled={busy} onClick={() => void remove(item)}>×</button>
                </div>
              </article>
            ))}
          </div>
        </aside>
      ) : null}
    </div>
  );
}
