import { useCallback, useEffect, useState } from 'react';

export interface FetchState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

// Fetch one JSON endpoint. With intervalMs > 0 the view polls (used by the
// live-leases view); the returned reload() forces an immediate refetch.
export function useFetch<T>(path: string, intervalMs?: number): FetchState<T> {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({
    data: null,
    error: null,
    loading: true,
  });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function load(): Promise<void> {
      try {
        const res = await fetch(path, {
          signal: controller.signal,
          headers: { Accept: 'application/json' },
        });
        if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
        const data = (await res.json()) as T;
        if (!cancelled) setState({ data, error: null, loading: false });
      } catch (err) {
        if (!cancelled && (err as Error).name !== 'AbortError') {
          setState({ data: null, error: (err as Error).message, loading: false });
        }
      }
    }

    void load();
    if (intervalMs && intervalMs > 0) {
      const id = window.setInterval(() => void load(), intervalMs);
      return () => {
        cancelled = true;
        window.clearInterval(id);
        controller.abort();
      };
    }
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [path, intervalMs, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}
