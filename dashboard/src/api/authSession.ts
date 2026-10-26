type Principal = {
  id: number;
  username: string;
  role: string;
  permissions?: readonly string[];
} | null;

let epoch = 0;
let principalKey: string | null | undefined;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of [...listeners]) listener();
}

export function getAuthSessionEpoch(): number {
  return epoch;
}

export function subscribeAuthSession(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function invalidateAuthSession(): number {
  epoch += 1;
  principalKey = undefined;
  notify();
  return epoch;
}

export function observeAuthPrincipal(principal: Principal): number {
  const nextKey = principal
    ? JSON.stringify([
        principal.id,
        principal.username,
        principal.role,
        [...(principal.permissions ?? [])].sort(),
      ])
    : null;
  if (principalKey === undefined) {
    principalKey = nextKey;
    return epoch;
  }
  if (principalKey !== nextKey) {
    epoch += 1;
    principalKey = nextKey;
    notify();
  }
  return epoch;
}

export function isAuthSessionCurrent(capturedEpoch: number): boolean {
  return epoch === capturedEpoch;
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key === "auth_token") invalidateAuthSession();
  });
}
