/** AnotAI mobile — acompanha a geração da nota após upload até processed ou falha. */

import { useEffect, useRef, useState } from "react";

import axios from "axios";
import { getNote } from "../services/api";
import type { Note } from "../types";

export type PollState = "idle" | "polling" | "done" | "timeout" | "error";

interface Options {
  intervalMs?: number;
  maxAttempts?: number;
}

export function useNotePolling(noteId: string | null, opts: Options = {}): {
  note: Note | null;
  state: PollState;
  error: string | null;
} {
  const { intervalMs = 2000, maxAttempts = 60 } = opts;
  const [note, setNote] = useState<Note | null>(null);
  const [state, setState] = useState<PollState>("idle");
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!noteId) {
      setNote(null);
      setState("idle");
      return;
    }
    let cancelled = false;
    let attempts = 0;
    setState("polling");
    setError(null);

    const tick = async (): Promise<void> => {
      try {
        const fresh = await getNote(noteId);
        if (cancelled) return;
        setNote(fresh);
        if (fresh.status === "processed") {
          setState("done");
          return;
        }
        // Falha terminal no backend (ex: pipeline/STT): encerrar com o motivo.
        const fatal = (fresh.logs ?? []).find(
          (l) => l.status === "failed" && (l.action_type === "pipeline" || l.action_type === "extraction"),
        );
        if (fatal) {
          setError(fatal.error_message || "Falha no processamento.");
          setState("error");
          return;
        }
        attempts += 1;
        if (attempts >= maxAttempts) {
          setState("timeout");
          return;
        }
        timer.current = setTimeout(tick, intervalMs);
      } catch (e) {
        if (!cancelled) {
          // Nota descartada no backend (ex: áudio sem transcrição): sem "Ver nota".
          const gone = axios.isAxiosError(e) && e.response?.status === 404;
          setError(gone ? "Áudio sem conteúdo aproveitável — grave novamente." : e instanceof Error ? e.message : "Falha ao consultar a nota");
          setState("error");
        }
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [noteId, intervalMs, maxAttempts]);

  return { note, state, error };
}
