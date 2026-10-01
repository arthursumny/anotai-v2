/**
 * AnotAI BLE — hook da lapela: status, gravação física, última nota e ações.
 *
 * Reconecta silenciosamente ao último dispositivo pareado ao montar e
 * re-anexa o AudioReceiver a cada (re)conexão.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { LapelAudioReceiver } from "./AudioReceiver";
import { getLapelManager, type ConnectionStatus } from "./BleManager";

export interface UseLapel {
  status: ConnectionStatus;
  hardwareRecording: boolean;
  lastNoteId: string | null;
  error: string | null;
  connect: () => Promise<void>;
  disconnect: () => Promise<void>;
  forget: () => Promise<void>;
  clearLastNote: () => void;
}

export function useLapel(): UseLapel {
  const [status, setStatus] = useState<ConnectionStatus>(getLapelManager().getStatus());
  const [hardwareRecording, setHardwareRecording] = useState(false);
  const [lastNoteId, setLastNoteId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const receiverRef = useRef<LapelAudioReceiver | null>(null);
  if (!receiverRef.current) receiverRef.current = new LapelAudioReceiver();

  useEffect(() => getLapelManager().onStatusChange(setStatus), []);

  // Tenta o pareamento persistente ao abrir o app (silencioso).
  useEffect(() => {
    void getLapelManager()
      .reconnectLast()
      .catch(() => undefined);
  }, []);

  // Anexa o receptor de áudio a cada conexão estabelecida.
  useEffect(() => {
    if (status !== "connected") {
      if (status !== "connecting" && status !== "scanning") {
        receiverRef.current?.abort();
        setHardwareRecording(false);
      }
      return;
    }
    const device = getLapelManager().getDevice();
    if (!device) return;
    let cancelled = false;
    let detach: (() => void) | null = null;
    receiverRef
      .current!.attach(device, {
        onHardwareRecordingStarted: () => {
          if (cancelled) return;
          setLastNoteId(null);
          setHardwareRecording(true);
        },
        onHardwareRecordingFinished: (noteId) => {
          if (cancelled) return;
          setHardwareRecording(false);
          setLastNoteId(noteId);
        },
        onError: (msg) => {
          if (!cancelled) setError(msg);
        },
      })
      .then((d) => {
        if (cancelled) d();
        else detach = d;
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Falha ao monitorar lapela");
      });
    return () => {
      cancelled = true;
      detach?.();
    };
  }, [status]);

  const connect = useCallback(async (): Promise<void> => {
    setError(null);
    try {
      await getLapelManager().scanAndConnect();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Falha ao conectar na lapela");
    }
  }, []);

  const disconnect = useCallback(async (): Promise<void> => {
    setHardwareRecording(false);
    await getLapelManager().disconnect();
  }, []);

  const forget = useCallback(async (): Promise<void> => {
    setHardwareRecording(false);
    setLastNoteId(null);
    await getLapelManager().forgetDevice();
  }, []);

  const clearLastNote = useCallback((): void => setLastNoteId(null), []);

  return { status, hardwareRecording, lastNoteId, error, connect, disconnect, forget, clearLastNote };
}
