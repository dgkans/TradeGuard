import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useApi } from "../api";
import { ErrorNote } from "./common";

/** Owner-only admin login. The password lives in the API's .env (ADMIN_PASSWORD). */
export function AdminLogin({ open, onClose, onLoggedIn }: {
  open: boolean; onClose: () => void; onLoggedIn: () => void;
}) {
  const api = useApi();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const titleId = useId();
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  if (!open) return null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.adminLogin(password);
      setPassword("");
      onLoggedIn();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <form className="dialog dialog-small" role="dialog" aria-modal="true" aria-labelledby={titleId}
            onClick={(event) => event.stopPropagation()} onSubmit={(event) => void submit(event)}>
        <div className="dialog-head">
          <h2 id={titleId}>Admin</h2>
          <button type="button" className="button button-quiet" onClick={onClose}>Close</button>
        </div>
        <p className="muted small">
          For the owner: all traders' approvals, the audit log and test results.
        </p>
        <label htmlFor={inputId} className="field-label">Password</label>
        <input id={inputId} ref={inputRef} className="input" type="password"
               autoComplete="current-password" value={password}
               onChange={(event) => setPassword(event.target.value)} />
        <div className="actions">
          <button type="submit" className="button" disabled={busy || !password}>Log in</button>
        </div>
        <ErrorNote error={error} />
      </form>
    </div>
  );
}
