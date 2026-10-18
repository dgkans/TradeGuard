import { useState } from "react";

const KEY = "tradeguard.reviewer";

function read(): string {
  try {
    return window.localStorage.getItem(KEY) || "operator";
  } catch {
    return "operator";
  }
}

/** The name recorded as `decided_by` on approvals. Remembered in this browser only. */
export function useReviewer(): [string, (name: string) => void] {
  const [name, setName] = useState(read);
  function update(next: string) {
    setName(next);
    try {
      window.localStorage.setItem(KEY, next);
    } catch {
      // Private mode or blocked storage: keep it for this page only.
    }
  }
  return [name, update];
}

export function reviewerName(name: string): string {
  return name.trim() || "operator";
}
