"use client";

import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQueryClient,
  type UseMutationOptions,
} from "@tanstack/react-query";
import { useState } from "react";

/** Single client per browser tab, recreated only on hot reload. */
export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** Wrap `useMutation` and auto-invalidate one or more query keys on success. */
export function useMutationWithInvalidate<TData, TVariables>(
  mutationFn: (vars: TVariables) => Promise<TData>,
  invalidate: (readonly unknown[])[],
  options?: Omit<UseMutationOptions<TData, Error, TVariables>, "mutationFn">,
) {
  const qc = useQueryClient();
  return useMutation<TData, Error, TVariables>({
    mutationFn,
    ...options,
    onSuccess: (data, vars, ctx, mutation) => {
      for (const queryKey of invalidate) qc.invalidateQueries({ queryKey });
      options?.onSuccess?.(data, vars, ctx, mutation);
    },
  });
}
