import { useCallback, useEffect, useMemo, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError } from "../lib/api";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type Account = {
  id: string;
  name: string;
  type: string;
  currency: string;
  offbudget: boolean;
  closed: boolean;
  ledger_balance: number;
};

type Category = { id: string; name: string; is_income: boolean };

type Tx = {
  id: string;
  date: string;
  amount: number;
  payee: string | null;
  category: string | null;
  account: string;
  notes: string | null;
  cleared: boolean;
  reconciled: boolean;
  transfer: boolean;
  currency?: string;
  split?: boolean;
  split_child?: boolean;
};

type Goal = {
  type: string;
  target: number;
  needed?: number;
  needed_per_month?: number;
  progress: number | null;
};

type BudgetRow = {
  id: string;
  name: string;
  is_income: boolean;
  budgeted: number;
  activity: number;
  available: number;
  rollover: number;
  rollover_mode?: string;
  goal?: Goal | null;
};

type BudgetBody = {
  month: string;
  base_currency: string;
  income: number;
  budgeted: number;
  available_to_budget: number;
  categories: BudgetRow[];
};

type RuleRow = {
  id: string;
  rank: number;
  conditions: Array<{ field: string; op: string; value: unknown }>;
  actions: Array<{ field: string; value: unknown }>;
  enabled: boolean;
};

const TABS = ["overview", "transactions", "budget", "schedules", "rules", "import", "bank", "reports"] as const;
type Tab = (typeof TABS)[number];

function openPicker(e: Event) {
  const el = e.target as HTMLInputElement;
  try {
    el.showPicker();
  } catch {
    // Browser without showPicker or without user activation: the
    // indicator stays the fallback entry point.
  }
}

function money(cents: number, currency: string = "USD"): string {
  return (cents / 100).toLocaleString(undefined, {
    style: "currency",
    currency,
  });
}

function currentMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeaderIfPresent(),
      ...(init?.headers ?? {}),
    },
  });
  if (!r.ok) throw await describeError(r);
  return (await r.json()) as T;
}

export function FinanceView() {
  const [tab, setTab] = useState<Tab>(() =>
    typeof window !== "undefined" &&
    new URLSearchParams(window.location.search).get("link")
      ? "bank"
      : "overview"
  );
  const [month, setMonth] = useState(currentMonth());
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [categories, setCategories] = useState<Category[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [a, c] = await Promise.all([
        api<{ accounts: Account[] }>("/finance/accounts"),
        api<{ categories: Category[] }>("/finance/categories"),
      ]);
      setAccounts(a.accounts);
      setCategories(c.categories);
      setError(null);
    } catch (e) {
      setError(describeError(e).message);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  if (error) return <ErrorState message={error} />;
  if (accounts === null || categories === null) return <Loading />;

  return (
    <div class="finance-view product-page">
      <header class="settings-page-heading">
        <div>
          <h1>Finance</h1>
          <p>Envelope budgeting, bank sync, and reports. Your data, your account.</p>
        </div>
        <div class="settings-summary">
          <span>Opt-in page · amounts in your base currency</span>
        </div>
      </header>
      <div class="finance-head">
        <input
          type="month"
          value={month}
          onInput={(e) => setMonth((e.target as HTMLInputElement).value)}
          onClick={openPicker}
          aria-label="Month"
        />
        <nav class="finance-tabs">
          {TABS.map((t) => (
            <button
              key={t}
              class={t === tab ? "finance-tab active" : "finance-tab"}
              onClick={() => setTab(t)}
            >
              {t}
            </button>
          ))}
        </nav>
      </div>
      {tab === "overview" ? <Overview accounts={accounts} month={month} /> : null}
      {tab === "transactions" ? (
        <Transactions month={month} accounts={accounts} categories={categories} />
      ) : null}
      {tab === "budget" ? (
        <Budget month={month} categories={categories} onChanged={reload} />
      ) : null}
      {tab === "schedules" ? <SchedulesTab /> : null}
      {tab === "rules" ? <Rules onChanged={reload} /> : null}
      {tab === "import" ? (
        <Importer accounts={accounts} categories={categories} onChanged={reload} />
      ) : null}
      {tab === "bank" ? <BankTab onChanged={reload} /> : null}
      {tab === "reports" ? <ReportsTab month={month} /> : null}
    </div>
  );
}

function Overview({ accounts, month }: { accounts: Account[]; month: string }) {
  const [body, setBody] = useState<BudgetBody | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api<BudgetBody>(`/finance/budget?month=${month}`)
      .then(setBody)
      .catch((e) => setErr(describeError(e).message));
  }, [month]);
  if (err) return <ErrorState message={err} />;
  if (!body) return <Loading />;
  return (
    <section class="surface-card settings-card finance-card">
      <div class="section-heading">
        <div><p class="eyebrow">Ledger</p><h2>Accounts</h2></div>
      </div>
      <table class="coverage">
        <thead>
          <tr>
            <th>Account</th>
            <th class="num">Ledger balance</th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((a) => (
            <tr key={a.id}>
              <td>
                {a.name} <small>({a.type}, {a.currency})</small>
              </td>
              <td class="num">{money(a.ledger_balance, a.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>
        {month} ({body.base_currency}): income {money(body.income, body.base_currency)} ·
        budgeted {money(body.budgeted, body.base_currency)} ·
        available to budget <strong>{money(body.available_to_budget, body.base_currency)}</strong>
      </p>
    </section>
  );
}

function Transactions({
  month,
  accounts,
  categories,
}: {
  month: string;
  accounts: Account[];
  categories: Category[];
}) {
  const [txs, setTxs] = useState<Tx[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [adding, setAdding] = useState(false);
  const openAccounts = useMemo(
    () => accounts.filter((a) => !a.closed),
    [accounts]
  );
  const [form, setForm] = useState({ date: "", payee: "", amount: "", category: "", account: "" });
  const [splits, setSplits] = useState<Array<{ amount: string; category: string }>>([]);
  const [formErr, setFormErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const search = new URLSearchParams();
      search.set("month", month);
      if (filter) search.set("q", filter);
      const body = await api<{ transactions: Tx[] }>(
        `/finance/transactions?${search.toString()}`
      );
      setTxs(body.transactions);
      setErr(null);
    } catch (e) {
      setErr(describeError(e).message);
    }
  }, [month, filter]);

  useEffect(() => {
    void load();
  }, [load]);

  const setCategory = async (tx: Tx, categoryId: string) => {
    await api(`/finance/transactions/${tx.id}`, {
      method: "PATCH",
      body: JSON.stringify({ category_id: categoryId || null }),
    });
    await load();
  };

  const submit = async () => {
    const accountId = form.account || openAccounts[0]?.id;
    if (!accountId || !form.date || !form.amount) return;
    setFormErr(null);
    const amount = Math.round(Number(form.amount) * 100);
    const payload: Record<string, unknown> = {
      account_id: accountId,
      date: form.date,
      amount,
      payee: form.payee || null,
      category_id: form.category || null,
    };
    if (splits.length) {
      const splitAmounts = splits
        .filter((s) => s.amount && s.category)
        .map((s) => ({
          amount: Math.round(Number(s.amount) * 100),
          category_id: s.category,
        }));
      const total = splitAmounts.reduce((acc, s) => acc + s.amount, 0);
      if (total !== amount) {
        setFormErr(`splits sum to ${(total / 100).toFixed(2)}, amount is ${form.amount}`);
        return;
      }
      payload["splits"] = splitAmounts;
      delete payload["category_id"];
    }
    try {
      await api("/finance/transactions", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setForm({ date: "", payee: "", amount: "", category: "", account: accountId });
      setSplits([]);
      setAdding(false);
      await load();
    } catch (e) {
      setFormErr(describeError(e).message);
    }
  };

  if (err) return <ErrorState message={err} />;
  if (!txs) return <Loading />;

  return (
    <section class="surface-card settings-card finance-card">
      <div class="section-heading">
        <div><p class="eyebrow">{month}</p><h2>Transactions</h2></div>
      </div>
      <div class="finance-form">
        <input
          placeholder="search payee / notes"
          value={filter}
          onInput={(e) => setFilter((e.target as HTMLInputElement).value)}
        />
        <button onClick={() => setAdding(!adding)}>Add transaction</button>
      </div>
      {adding ? (
        <div class="finance-form">
          <select
            value={
              openAccounts.some((a) => a.id === form.account) || openAccounts.length === 0
                ? form.account
                : openAccounts[0].id
            }
            onChange={(e) => setForm({ ...form, account: (e.target as HTMLSelectElement).value })}
          >
            {openAccounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} ({a.currency})
              </option>
            ))}
          </select>
          <input
            type="date"
            value={form.date}
            onInput={(e) => setForm({ ...form, date: (e.target as HTMLInputElement).value })}
            onClick={openPicker}
          />
          <input
            placeholder="payee"
            value={form.payee}
            onInput={(e) => setForm({ ...form, payee: (e.target as HTMLInputElement).value })}
          />
          <input
            type="number"
            step="0.01"
            placeholder="amount"
            value={form.amount}
            onInput={(e) => setForm({ ...form, amount: (e.target as HTMLInputElement).value })}
          />
          {!splits.length ? (
            <select
              value={form.category}
              onChange={(e) => setForm({ ...form, category: (e.target as HTMLSelectElement).value })}
            >
              <option value="">category…</option>
              {categories.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          ) : null}
          <button
            onClick={() => setSplits([...splits, { amount: "", category: "" }])}
          >
            Add split
          </button>
          <button
            disabled={!form.date || !form.amount}
            onClick={() => void submit()}
          >
            Save
          </button>
          {formErr ? <small>{formErr}</small> : null}
          {splits.map((split, idx) => (
            <div class="finance-form" key={idx}>
              <input
                type="number"
                step="0.01"
                placeholder={`split ${idx + 1} amount`}
                value={split.amount}
                onInput={(e) => {
                  const next = [...splits];
                  next[idx] = { ...split, amount: (e.target as HTMLInputElement).value };
                  setSplits(next);
                }}
              />
              <select
                value={split.category}
                onChange={(e) => {
                  const next = [...splits];
                  next[idx] = { ...split, category: (e.target as HTMLSelectElement).value };
                  setSplits(next);
                }}
              >
                <option value="">category…</option>
                {categories.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
              <button
                onClick={() => setSplits(splits.filter((_, i) => i !== idx))}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      ) : null}
      <table class="coverage">
        <thead>
          <tr>
            <th>Date</th>
            <th>Payee</th>
            <th class="num">Amount</th>
            <th>Category</th>
            <th>Account</th>
          </tr>
        </thead>
        <tbody>
          {txs.map((tx) => (
            <tr key={tx.id}>
              <td>{tx.date}</td>
              <td>
                <span style={tx.split_child ? "padding-left:12px" : ""}>
                  {tx.payee ?? tx.notes ?? "—"}
                  {tx.split ? <small> split</small> : null}
                  {tx.transfer ? <small> transfer</small> : null}
                </span>
              </td>
              <td class="num">{money(tx.amount, tx.currency)}</td>
              <td>
                {tx.split ? (
                  <small>see parts</small>
                ) : (
                  <select
                    value={categories.find((c) => c.name === tx.category)?.id ?? ""}
                    disabled={tx.reconciled}
                    onChange={(e) =>
                      void setCategory(tx, (e.target as HTMLSelectElement).value)
                    }
                  >
                    <option value="">—</option>
                    {categories.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                )}
              </td>
              <td>{tx.account}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Budget({
  month,
  categories,
  onChanged,
}: {
  month: string;
  categories: Category[];
  onChanged(): void;
}) {
  const [body, setBody] = useState<BudgetBody | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [newCat, setNewCat] = useState("");
  const [newIncome, setNewIncome] = useState(false);

  const load = useCallback(async () => {
    try {
      setBody(await api<BudgetBody>(`/finance/budget?month=${month}`));
      setErr(null);
    } catch (e) {
      setErr(describeError(e).message);
    }
  }, [month]);

  useEffect(() => {
    void load();
  }, [load]);

  const setAmount = async (categoryId: string, cents: number, mode?: string) => {
    await api("/finance/budget", {
      method: "PUT",
      body: JSON.stringify({
        month,
        category_id: categoryId,
        amount: cents,
        rollover_mode: mode,
      }),
    });
    await load();
  };

  const addCategory = async () => {
    if (!newCat.trim()) return;
    await api("/finance/categories", {
      method: "POST",
      body: JSON.stringify({ name: newCat.trim(), is_income: newIncome }),
    });
    setNewCat("");
    setNewIncome(false);
    onChanged();
  };

  if (err) return <ErrorState message={err} />;
  if (!body) return <Loading />;

  return (
    <section class="surface-card settings-card finance-card">
      <div class="section-heading">
        <div><p class="eyebrow">{month} · {body.base_currency}</p><h2>Budget</h2></div>
      </div>
      <table class="coverage">
        <thead>
          <tr>
            <th>Category</th>
            <th class="num">Budgeted</th>
            <th class="num">Activity</th>
            <th class="num">Rollover</th>
            <th class="num">Available</th>
            <th>Goal</th>
            <th>Rollover mode</th>
          </tr>
        </thead>
        <tbody>
          {body.categories.map((c) => {
            const known = categories.some((k) => k.id === c.id && k.is_income);
            return (
              <tr key={c.id}>
                <td>{c.name}</td>
                <td class="num">
                  {known ? (
                    "—"
                  ) : (
                    <input
                      type="number"
                      step="0.01"
                      value={(c.budgeted / 100).toFixed(2)}
                      onBlur={(e) => {
                        const dollars = Number(
                          (e.target as HTMLInputElement).value
                        );
                        if (Number.isFinite(dollars)) {
                          void setAmount(
                            c.id,
                            Math.round(dollars * 100)
                          );
                        }
                      }}
                    />
                  )}
                </td>
                <td class="num">{money(c.activity)}</td>
                <td class="num">{known ? "—" : money(c.rollover)}</td>
                <td class="num">{known ? "—" : money(c.available)}</td>
                <td>
                  {c.goal
                    ? c.goal.type === "monthly"
                      ? `${money(c.goal.target)} / mo, need ${money(c.goal.needed ?? 0)}`
                      : `${money(c.goal.target)} by ${Math.round((c.goal.progress ?? 0) * 100)}%`
                    : "—"}
                </td>
                <td>
                  {known ? (
                    "—"
                  ) : (
                    <select
                      value={c.rollover_mode ?? "rollover"}
                      onChange={(e) =>
                        void setAmount(
                          c.id,
                          c.budgeted,
                          (e.target as HTMLSelectElement).value
                        )
                      }
                    >
                      <option value="rollover">rollover</option>
                      <option value="reset">reset</option>
                      <option value="hold">hold</option>
                    </select>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p>
        available to budget: <strong>{money(body.available_to_budget, body.base_currency)}</strong>
      </p>
      <div class="finance-form">
        <input
          placeholder="new category"
          value={newCat}
          onInput={(e) => setNewCat((e.target as HTMLInputElement).value)}
        />
        <label>
          <input
            type="checkbox"
            checked={newIncome}
            onChange={(e) =>
              setNewIncome((e.target as HTMLInputElement).checked)
            }
          />{" "}
          income
        </label>
        <button disabled={!newCat.trim()} onClick={() => void addCategory()}>Add</button>
      </div>
    </section>
  );
}

function Rules({ onChanged }: { onChanged(): void }) {
  const [rules, setRules] = useState<RuleRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [payee, setPayee] = useState("");
  const [category, setCategory] = useState("");
  const [categories, setCategories] = useState<Category[]>([]);

  const load = useCallback(async () => {
    try {
      const [r, c] = await Promise.all([
        api<{ rules: RuleRow[] }>("/finance/rules"),
        api<{ categories: Category[] }>("/finance/categories"),
      ]);
      setRules(r.rules);
      setCategories(c.categories);
      setErr(null);
    } catch (e) {
      setErr(describeError(e).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const add = async () => {
    if (!payee.trim() || !category) return;
    await api("/finance/rules", {
      method: "POST",
      body: JSON.stringify({
        conditions: [
          { field: "imported_payee", op: "contains", value: payee.trim() },
        ],
        actions: [{ field: "category", value: category }],
      }),
    });
    setPayee("");
    await load();
    onChanged();
  };

  const remove = async (id: string) => {
    await api(`/finance/rules/${id}`, { method: "DELETE" });
    await load();
  };

  if (err) return <ErrorState message={err} />;
  if (!rules) return <Loading />;

  return (
    <section class="surface-card settings-card finance-card">
      <div class="section-heading">
        <div><p class="eyebrow">Automation</p><h2>Rules</h2></div>
      </div>
      <table class="coverage">
        <thead>
          <tr>
            <th>#</th>
            <th>When imported payee contains</th>
            <th>Set category</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rules.map((r) => {
            const cond = r.conditions[0];
            const act = r.actions[0];
            return (
              <tr key={r.id}>
                <td>{r.rank}</td>
                <td>{String(cond?.value ?? "")}</td>
                <td>{String(act?.value ?? "")}</td>
                <td>
                  <button onClick={() => void remove(r.id)}>Remove</button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div class="finance-form">
        <input
          placeholder="payee contains"
          value={payee}
          onInput={(e) => setPayee((e.target as HTMLInputElement).value)}
        />
        <select
          value={category}
          onChange={(e) => setCategory((e.target as HTMLSelectElement).value)}
        >
          <option value="">category…</option>
          {categories.map((c) => (
            <option key={c.id} value={c.name}>
              {c.name}
            </option>
          ))}
        </select>
        <button disabled={!payee.trim() || !category} onClick={() => void add()}>
          Add rule
        </button>
      </div>
    </section>
  );
}

function Importer({
  accounts,
  categories,
  onChanged,
}: {
  accounts: Account[];
  categories: Category[];
  onChanged(): void;
}) {
  const open = useMemo(() => accounts.filter((a) => !a.closed), [accounts]);
  const [pickedAccount, setPickedAccount] = useState("");
  const accountId =
    open.some((a) => a.id === pickedAccount) || open.length === 0
      ? pickedAccount
      : open[0].id;
  const [csv, setCsv] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [newAccount, setNewAccount] = useState("");
  const [newType, setNewType] = useState("checking");

  const run = async (preview: boolean) => {
    if (!accountId || !csv.trim()) return;
    setBusy(true);
    try {
      const body = await api<{
        added: string[];
        updated: string[];
        preview: Array<{ ignored?: string }>;
      }>("/finance/import", {
        method: "POST",
        body: JSON.stringify({ account_id: accountId, csv, preview }),
      });
      const ignored = body.preview.filter((p) => p.ignored).length;
      setResult(
        `${preview ? "preview: " : ""}added ${body.added.length}, updated ${body.updated.length}, ignored ${ignored}`
      );
      setErr(null);
      if (!preview) {
        setCsv("");
        onChanged();
      }
    } catch (e) {
      setErr(describeError(e).message);
    } finally {
      setBusy(false);
    }
  };

  const addAccount = async () => {
    if (!newAccount.trim()) return;
    const body = await api<{ id: string }>("/finance/accounts", {
      method: "POST",
      body: JSON.stringify({ name: newAccount.trim(), type: newType }),
    });
    setNewAccount("");
    setPickedAccount(body.id);
    onChanged();
  };

  return (
    <section class="surface-card settings-card finance-card">
      <div class="section-heading">
        <div><p class="eyebrow">Files</p><h2>Import</h2></div>
      </div>
      <div class="finance-form">
        <select
          value={accountId}
          onChange={(e) => setPickedAccount((e.target as HTMLSelectElement).value)}
        >
          {open.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
        <input
          placeholder="new account name"
          value={newAccount}
          onInput={(e) => setNewAccount((e.target as HTMLInputElement).value)}
        />
        <select
          value={newType}
          onChange={(e) => setNewType((e.target as HTMLSelectElement).value)}
        >
          {["checking", "savings", "credit", "loan", "cash", "investment", "other"].map(
            (t) => (
              <option key={t} value={t}>
                {t}
              </option>
            )
          )}
        </select>
        <button onClick={() => void addAccount()}>Add account</button>
      </div>
      <p>
        <small>
          CSV needs date + amount (or debit/credit) columns; payee/notes/reference
          recognised when present. {categories.length} categories ready for rules.
        </small>
      </p>
      <textarea
        rows={8}
        placeholder={"Date,Description,Amount\n2026-01-05,KROGER,-42.10"}
        value={csv}
        onInput={(e) => setCsv((e.target as HTMLTextAreaElement).value)}
      />
      <div class="finance-form">
        <button disabled={busy || !accountId || !csv.trim()} onClick={() => void run(true)}>
          Preview
        </button>
        <button disabled={busy || !accountId || !csv.trim()} onClick={() => void run(false)}>
          Import
        </button>
        {!open.length ? (
          <small>add an account above before importing</small>
        ) : null}
      </div>
      {result ? <p>{result}</p> : null}
      {err ? <ErrorState message={err} /> : null}
      <ActualMigration onChanged={onChanged} />
    </section>
  );
}

function ActualMigration({ onChanged }: { onChanged(): void }) {
  const [report, setReport] = useState<Record<string, number> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const upload = async (file: File) => {
    setBusy(true);
    try {
      const buffer = await file.arrayBuffer();
      let binary = "";
      const bytes = new Uint8Array(buffer);
      for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
      const body = await api<Record<string, number>>("/finance/migrate/actual", {
        method: "POST",
        body: JSON.stringify({
          zip_base64: btoa(binary),
        }),
      });
      setReport(body);
      setErr(null);
      onChanged();
    } catch (e) {
      setErr(describeError(e).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="finance-form">
      <label class="finance-file-button">
        <input
          type="file"
          accept=".zip"
          disabled={busy}
          onChange={(e) => {
            const file = (e.target as HTMLInputElement).files?.[0];
            if (file) void upload(file);
          }}
        />
        <span>{busy ? "Importing…" : "Upload Actual Budget backup (.zip)"}</span>
      </label>
      {report ? (
        <small>
          migrated: {report.accounts ?? 0} accounts, {report.categories ?? 0} categories,{" "}
          {report.payees ?? 0} payees, {report.transactions ?? 0} transactions,{" "}
          {report.schedules ?? 0} schedules, {report.rules ?? 0} rules
          {report.deleted_skipped ? `, ${report.deleted_skipped} deleted skipped` : ""}
          {report.skipped_rules ? `, ${report.skipped_rules} unusable rules skipped` : ""}
        </small>
      ) : null}
      {err ? <small>{err}</small> : null}
    </div>
  );
}

type BankProviderInfo = {
  key: string;
  label: string;
  fields: string[];
  configured: boolean;
};

type BankAccountInfo = {
  id: string;
  provider: string;
  name: string;
  last_synced_at: string | null;
  sync_error: string | null;
};

function BankTab({ onChanged }: { onChanged(): void }) {
  const [providers, setProviders] = useState<BankProviderInfo[] | null>(null);
  const [accounts, setAccounts] = useState<BankAccountInfo[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [gcId, setGcId] = useState("");
  const [gcKey, setGcKey] = useState("");
  const [sfEmail, setSfEmail] = useState("");
  const [sfPassword, setSfPassword] = useState("");
  const [country, setCountry] = useState("");
  const [institutions, setInstitutions] = useState<Array<{ id: string; name: string }> | null>(null);
  const [linkUrl, setLinkUrl] = useState<string | null>(null);
  const [linkId, setLinkId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, a] = await Promise.all([
        api<{ providers: BankProviderInfo[] }>("/finance/bank/credentials"),
        api<{ accounts: BankAccountInfo[] }>("/finance/bank/accounts"),
      ]);
      setProviders(p.providers);
      setAccounts(a.accounts);
      setErr(null);
    } catch (e) {
      setErr(describeError(e).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // The bank's approval flow redirects back to /finance?link=<id>; pick
  // the id up and poll the requisition automatically.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const link = params.get("link");
    if (!link) return;
    setLinkId(link);
    let tries = 0;
    const timer = window.setInterval(async () => {
      tries += 1;
      try {
        const body = await api<{ status: string; accounts: unknown[] }>(
          `/finance/bank/link/${link}`
        );
        if (body.status === "LN" || tries >= 10) {
          window.clearInterval(timer);
          setMsg(
            body.status === "LN"
              ? `Bank linked (${body.accounts.length} accounts)`
              : `Bank link still ${body.status}`
          );
          await load();
          window.history.replaceState(null, "", "/finance");
        }
      } catch {
        if (tries >= 10) window.clearInterval(timer);
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, []);

  const saveGocardless = async () => {
    setBusy(true);
    try {
      await api("/finance/bank/credentials", {
        method: "PUT",
        body: JSON.stringify({
          provider: "gocardless",
          fields: { secret_id: gcId, secret_key: gcKey },
        }),
      });
      setGcId("");
      setGcKey("");
      setMsg("GoCardless credentials stored, encrypted.");
      await load();
    } catch (e) {
      setErr(describeError(e).message);
    } finally {
      setBusy(false);
    }
  };

  const saveSimplefin = async () => {
    setBusy(true);
    try {
      await api("/finance/bank/credentials", {
        method: "PUT",
        body: JSON.stringify({
          provider: "simplefin",
          fields: { email: sfEmail, password: sfPassword },
        }),
      });
      setSfEmail("");
      setSfPassword("");
      setMsg("SimpleFIN linked.");
      await load();
      await api("/finance/bank/simplefin/link", { method: "POST" });
      await load();
      onChanged();
    } catch (e) {
      setErr(describeError(e).message);
    } finally {
      setBusy(false);
    }
  };

  const listInstitutions = async () => {
    setBusy(true);
    try {
      const body = await api<{ institutions: Array<{ id: string; name: string }> }>(
        `/finance/bank/institutions?country=${encodeURIComponent(country)}`
      );
      setInstitutions(body.institutions);
      setErr(null);
    } catch (e) {
      setErr(describeError(e).message);
    } finally {
      setBusy(false);
    }
  };

  const startLink = async (institutionId: string) => {
    setBusy(true);
    try {
      const body = await api<{ link_id: string; url: string }>("/finance/bank/link", {
        method: "POST",
        body: JSON.stringify({ institution_id: institutionId }),
      });
      setLinkId(body.link_id);
      setLinkUrl(body.url);
      setErr(null);
    } catch (e) {
      setErr(describeError(e).message);
    } finally {
      setBusy(false);
    }
  };

  const pollLink = async () => {
    if (!linkId) return;
    setBusy(true);
    try {
      const body = await api<{ status: string; accounts: unknown[] }>(
        `/finance/bank/link/${linkId}`
      );
      setMsg(`Bank link status: ${body.status} (${body.accounts.length} accounts)`);
      await load();
      onChanged();
    } catch (e) {
      setErr(describeError(e).message);
    } finally {
      setBusy(false);
    }
  };

  const sync = async (id?: string) => {
    setBusy(true);
    try {
      const body = await api<{ synced: Record<string, { added: number; updated: number }>; errors: Record<string, string> }>(
        "/finance/bank/sync",
        { method: "POST", body: JSON.stringify(id ? { bank_account_id: id } : {}) }
      );
      const totals = Object.values(body.synced).reduce(
        (acc, r) => ({ added: acc.added + r.added, updated: acc.updated + r.updated }),
        { added: 0, updated: 0 }
      );
      const errCount = Object.keys(body.errors).length;
      setMsg(
        `Sync: ${totals.added} added, ${totals.updated} updated` +
          (errCount ? ` (${errCount} failed)` : "")
      );
      await load();
      onChanged();
    } catch (e) {
      setErr(describeError(e).message);
    } finally {
      setBusy(false);
    }
  };

  if (err && !providers) return <ErrorState message={err} />;
  if (!providers || !accounts) return <Loading />;
  const gc = providers.find((p) => p.key === "gocardless");
  const sf = providers.find((p) => p.key === "simplefin");

  return (
    <section class="surface-card settings-card finance-card">
      <div class="section-heading">
        <div><p class="eyebrow">Connections</p><h2>Bank sync</h2></div>
      </div>
      {err ? <p class="empty">{err}</p> : null}
      {msg ? <p>{msg}</p> : null}

      <div class="finance-form">
        {gc?.configured ? (
          <small>GoCardless: configured</small>
        ) : (
          <>
            <input
              placeholder="GoCardless secret_id"
              value={gcId}
              onInput={(e) => setGcId((e.target as HTMLInputElement).value)}
            />
            <input
              type="password"
              placeholder="secret_key"
              value={gcKey}
              onInput={(e) => setGcKey((e.target as HTMLInputElement).value)}
            />
            <button
              disabled={busy || !gcId.trim() || !gcKey.trim()}
              onClick={() => void saveGocardless()}
            >
              Save GoCardless
            </button>
          </>
        )}
      </div>
      {gc?.configured ? (
        <div class="finance-form">
          <input
            placeholder="country code (e.g. DE, FR)"
            value={country}
            onInput={(e) => setCountry((e.target as HTMLInputElement).value)}
          />
          <button disabled={busy} onClick={() => void listInstitutions()}>
            List banks
          </button>
        </div>
      ) : null}
      {institutions ? (
        <table class="coverage">
          <thead>
            <tr>
              <th>Bank</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {institutions.slice(0, 50).map((i) => (
              <tr key={i.id}>
                <td>{i.name}</td>
                <td>
                  <button disabled={busy} onClick={() => void startLink(i.id)}>
                    Link
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {linkUrl ? (
        <p>
          <a href={linkUrl} target="_blank" rel="noreferrer">
            Open your bank's approval page
          </a>{" "}
          then <button onClick={() => void pollLink()}>check status</button>
        </p>
      ) : null}

      <div class="finance-form">
        {sf?.configured ? (
          <small>SimpleFIN: linked</small>
        ) : (
          <>
            <input
              placeholder="SimpleFIN email"
              value={sfEmail}
              onInput={(e) => setSfEmail((e.target as HTMLInputElement).value)}
            />
            <input
              type="password"
              placeholder="password"
              value={sfPassword}
              onInput={(e) => setSfPassword((e.target as HTMLInputElement).value)}
            />
            <button
              disabled={busy || !sfEmail.trim() || !sfPassword.trim()}
              onClick={() => void saveSimplefin()}
            >
              Link SimpleFIN
            </button>
          </>
        )}
      </div>

      <table class="coverage">
        <thead>
          <tr>
            <th>Bank account</th>
            <th>Provider</th>
            <th>Last sync</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((a) => (
            <tr key={a.id}>
              <td>
                {a.name}
                {a.sync_error ? <small> {a.sync_error}</small> : null}
              </td>
              <td>{a.provider}</td>
              <td>{a.last_synced_at ?? "never"}</td>
              <td>
                <button disabled={busy} onClick={() => void sync(a.id)}>
                  Sync
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {accounts.length ? (
        <div class="finance-form">
          <button disabled={busy} onClick={() => void sync()}>
            Sync all
          </button>
        </div>
      ) : null}
    </section>
  );
}

type ScheduleRow = {
  id: string;
  name: string;
  amount: number | null;
  payee: string | null;
  account: string | null;
  status: string;
  next: string | null;
  active: boolean;
};

function SchedulesTab() {
  const [rows, setRows] = useState<ScheduleRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [payee, setPayee] = useState("");
  const [amount, setAmount] = useState("");
  const [day, setDay] = useState("1");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const body = await api<{ schedules: ScheduleRow[] }>("/finance/schedules");
      setRows(body.schedules);
      setErr(null);
    } catch (e) {
      setErr(describeError(e).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const add = async () => {
    setBusy(true);
    try {
      await api("/finance/schedules", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          payee: payee.trim() || null,
          amount: amount ? Math.round(Number(amount) * 100) : null,
          config: {
            frequency: "monthly",
            start: new Date().toISOString().slice(0, 10),
            patterns: [{ type: "dayOfMonth", value: Number(day) || 1 }],
          },
        }),
      });
      setName("");
      setPayee("");
      setAmount("");
      await load();
    } catch (e) {
      setErr(describeError(e).message);
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (row: ScheduleRow) => {
    await api(`/finance/schedules/${row.id}`, {
      method: "PATCH",
      body: JSON.stringify({ active: !row.active }),
    });
    await load();
  };

  const remove = async (id: string) => {
    await api(`/finance/schedules/${id}`, { method: "DELETE" });
    await load();
  };

  if (err) return <ErrorState message={err} />;
  if (!rows) return <Loading />;

  return (
    <section class="surface-card settings-card finance-card">
      <div class="section-heading">
        <div><p class="eyebrow">Recurring</p><h2>Schedules</h2></div>
      </div>
      <table class="coverage">
        <thead>
          <tr>
            <th>Schedule</th>
            <th>Payee</th>
            <th class="num">Amount</th>
            <th>Status</th>
            <th>Next</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td>{row.name}</td>
              <td>{row.payee ?? "—"}</td>
              <td class="num">{row.amount !== null ? money(row.amount) : "—"}</td>
              <td>{row.status}</td>
              <td>{row.next ?? "—"}</td>
              <td>
                <button onClick={() => void toggle(row)}>
                  {row.active ? "Pause" : "Resume"}
                </button>{" "}
                <button onClick={() => void remove(row.id)}>Remove</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div class="finance-form">
        <input
          placeholder="name (e.g. Rent)"
          value={name}
          onInput={(e) => setName((e.target as HTMLInputElement).value)}
        />
        <input
          placeholder="payee"
          value={payee}
          onInput={(e) => setPayee((e.target as HTMLInputElement).value)}
        />
        <input
          type="number"
          step="0.01"
          placeholder="amount"
          value={amount}
          onInput={(e) => setAmount((e.target as HTMLInputElement).value)}
        />
        <input
          type="number"
          min="1"
          max="31"
          placeholder="day of month"
          value={day}
          onInput={(e) => setDay((e.target as HTMLInputElement).value)}
        />
        <button disabled={busy || !name.trim()} onClick={() => void add()}>
          Add monthly schedule
        </button>
      </div>
    </section>
  );
}

function ReportsTab({ month }: { month: string }) {
  const [cash, setCash] = useState<{ months: Array<{ month: string; income: number; expense: number; net: number }> } | null>(null);
  const [spending, setSpending] = useState<{ categories: Array<{ category: string; total: number; tx_count: number }> } | null>(null);
  const [worth, setWorth] = useState<{ months: Array<{ month: string; net_worth: number | null; change: number | null }>; accounts_without_history: string[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api<{ months: Array<{ month: string; income: number; expense: number; net: number }> }>(
      "/finance/reports/cash-flow?months=6"
    ).then(setCash).catch((e) => setErr(describeError(e).message));
    api<{ categories: Array<{ category: string; total: number; tx_count: number }> }>(
      `/finance/reports/spending?month=${month}`
    ).then(setSpending).catch(() => setSpending({ categories: [] }));
    api<{ months: Array<{ month: string; net_worth: number | null; change: number | null }>; accounts_without_history: string[] }>(
      "/finance/reports/net-worth?months=12"
    ).then(setWorth).catch(() => {});
  }, [month]);

  if (err) return <ErrorState message={err} />;
  if (!cash) return <Loading />;

  const latestWorth = [...(worth?.months ?? [])].reverse().find((m) => m.net_worth !== null);

  return (
    <section class="surface-card settings-card finance-card">
      <div class="section-heading">
        <div><p class="eyebrow">Ledger</p><h2>Reports</h2></div>
      </div>
      {latestWorth ? (
        <p>
          Ledger net worth: <strong>{money(latestWorth.net_worth ?? 0)}</strong> (all
          accounts, transactions only)
        </p>
      ) : null}
      {worth?.accounts_without_history?.length ? (
        <p>
          <small>
            no transaction history: {worth.accounts_without_history.join(", ")}
          </small>
        </p>
      ) : null}
      <table class="coverage">
        <thead>
          <tr>
            <th>Month</th>
            <th class="num">Income</th>
            <th class="num">Expense</th>
            <th class="num">Net</th>
          </tr>
        </thead>
        <tbody>
          {cash.months.map((m) => (
            <tr key={m.month}>
              <td>{m.month.slice(0, 7)}</td>
              <td class="num">{money(m.income)}</td>
              <td class="num">{money(m.expense)}</td>
              <td class="num">{money(m.net)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {spending ? (
        <table class="coverage">
          <thead>
            <tr>
              <th>Category</th>
              <th class="num">Spending ({month.slice(0, 7)})</th>
              <th class="num">Transactions</th>
            </tr>
          </thead>
          <tbody>
            {spending.categories.map((c) => (
              <tr key={c.category}>
                <td>{c.category}</td>
                <td class="num">{money(c.total)}</td>
                <td class="num">{c.tx_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}
