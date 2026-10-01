import type { BottomTabScreenProps } from "@react-navigation/bottom-tabs";
import type { CompositeScreenProps } from "@react-navigation/native";
import type { NativeStackScreenProps } from "@react-navigation/native-stack";
import { Audio } from "expo-av";
import React, { useEffect, useRef, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { useNotePolling } from "../hooks/useNotePolling";
import { useLapel } from "../ble/useLapel";
import type { NotesStackParamList, TabsParamList } from "../navigation/RootNavigator";
import { apiErrorMessage, uploadAudio } from "../services/api";

type Props = CompositeScreenProps<
  BottomTabScreenProps<TabsParamList, "Record">,
  NativeStackScreenProps<NotesStackParamList>
>;

function formatElapsed(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export default function RecordScreen({ navigation }: Props): JSX.Element {
  const [recording, setRecording] = useState<Audio.Recording | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [acceptedNoteId, setAcceptedNoteId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const lapel = useLapel();
  // Nota vinda do celular ou da lapela — o polling acompanha qualquer uma.
  const polledNoteId = acceptedNoteId ?? lapel.lastNoteId;
  const { note, state: pollState, error: pollError } = useNotePolling(polledNoteId);

  useEffect(
    () => () => {
      if (timer.current) clearInterval(timer.current);
    },
    [],
  );

  const startRecording = async (): Promise<void> => {
    setError(null);
    setAcceptedNoteId(null);
    lapel.clearLastNote();
    try {
      const { status } = await Audio.requestPermissionsAsync();
      if (status !== "granted") {
        setError("Permissão de microfone negada. Libere nas configurações do sistema.");
        return;
      }
      await Audio.setAudioModeAsync({ allowsRecordingIOS: true, playsInSilentModeIOS: true });
      const rec = new Audio.Recording();
      await rec.prepareToRecordAsync(Audio.RecordingOptionsPresets.HIGH_QUALITY);
      await rec.startAsync();
      setRecording(rec);
      setElapsed(0);
      timer.current = setInterval(() => setElapsed((e) => e + 1), 1000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Falha ao iniciar a gravação");
    }
  };

  const stopAndUpload = async (): Promise<void> => {
    if (!recording) return;
    if (timer.current) clearInterval(timer.current);
    try {
      await recording.stopAndUnloadAsync();
      const uri = recording.getURI();
      setRecording(null);
      await Audio.setAudioModeAsync({ allowsRecordingIOS: false });
      if (!uri) {
        setError("Gravação sem arquivo de áudio");
        return;
      }
      setUploading(true);
      const accepted = await uploadAudio(uri);
      setAcceptedNoteId(accepted.note_id);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setUploading(false);
    }
  };

  const isRecording = recording !== null;
  const phase: "idle" | "uploading" | "transcribing" | "done" | "attention" =
    uploading || (polledNoteId && pollState === "idle")
      ? "uploading"
      : polledNoteId && pollState === "polling"
        ? "transcribing"
        : polledNoteId && pollState === "done"
          ? "done"
          : polledNoteId
            ? "attention"
            : "idle";

  const lapelLabel =
    lapel.status === "connected"
      ? "Lapela Conectada 🟢"
      : lapel.status === "disconnected"
        ? "Lapela Desconectada ⚪"
        : "Lapela: conectando… 🟡";

  const steps = [
    { key: "uploading", label: "Enviando…" },
    { key: "transcribing", label: "Transcrevendo…" },
    { key: "done", label: "Concluído!" },
  ] as const;
  const order = ["idle", "uploading", "transcribing", "done"] as const;
  const phaseRank = (p: string): number => {
    const i = (order as readonly string[]).indexOf(p === "attention" ? "transcribing" : p);
    return i < 0 ? 0 : i;
  };

  return (
    <View style={styles.container}>
      <Pressable
        style={styles.lapelRow}
        onPress={() => {
          if (lapel.status === "disconnected") void lapel.connect();
        }}
      >
        <Text style={styles.lapelText}>{lapelLabel}</Text>
        {lapel.status === "disconnected" && <Text style={styles.lapelAction}>Conectar lapela</Text>}
      </Pressable>
      {(lapel.error || error || pollError) && <Text style={styles.error}>{lapel.error ?? error ?? pollError}</Text>}

      {lapel.hardwareRecording && (
        <View style={styles.hwBanner}>
          <ActivityIndicator color="#fff" size="small" />
          <Text style={styles.hwBannerText}>Gravando pela lapela… acompanhe por aqui</Text>
        </View>
      )}

      <Text style={styles.status}>
        {isRecording ? `Gravando… ${formatElapsed(elapsed)}` : uploading ? "Enviando áudio…" : "Pronto para gravar"}
      </Text>

      <Pressable
        style={[styles.recBtn, isRecording && styles.recBtnActive]}
        onPress={isRecording ? stopAndUpload : startRecording}
        disabled={uploading}
      >
        {uploading ? (
          <ActivityIndicator color="#fff" size="large" />
        ) : (
          <Text style={styles.recText}>{isRecording ? "Parar" : "Gravar"}</Text>
        )}
      </Pressable>

      {phase !== "idle" && (
        <View style={styles.result}>
          {steps.map((s) => {
            const active = phaseRank(phase) >= phaseRank(s.key);
            const current = (phase === s.key) || (phase === "attention" && s.key === "transcribing");
            return (
              <View key={s.key} style={styles.stepRow}>
                {current && phase !== "done" ? (
                  <ActivityIndicator size="small" />
                ) : (
                  <View style={[styles.dot, active && styles.dotActive]} />
                )}
                <Text style={[styles.stepText, active && styles.stepTextActive]}>{s.label}</Text>
              </View>
            );
          })}
          {phase === "attention" && (
            <Text style={styles.attention}>
              {pollState === "timeout"
                ? "Ainda processando — abra a nota para acompanhar"
                : "Falha ao acompanhar — abra a nota para tentar de novo"}
            </Text>
          )}
          {note && (pollState === "done" || pollState === "timeout" || pollState === "error") && (
            <>
              {pollState === "done" && (
                <Text style={styles.done}>
                  {note.status === "processed"
                    ? "Nota processada com sucesso"
                    : "Sem template automático — você pode classificar manualmente"}
                </Text>
              )}
              {(note.raw_transcription || "").trim() !== "" ? (
                <Pressable
                  style={styles.openBtn}
                  onPress={() => navigation.navigate("Notes", { screen: "NoteDetail", params: { noteId: note.id } })}
                >
                  <Text style={styles.openText}>Ver nota</Text>
                </Pressable>
              ) : (
                <Text style={styles.attention}>Sem transcrição — grave novamente com som.</Text>
              )}
            </>
          )}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", padding: 24, backgroundColor: "#fff" },
  lapelRow: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: "#f3f4f6",
    borderRadius: 999,
    paddingHorizontal: 14,
    paddingVertical: 8,
    marginBottom: 12,
  },
  lapelText: { fontWeight: "700", color: "#374151" },
  lapelAction: { color: "#2563eb", fontWeight: "700", marginLeft: 8 },
  hwBanner: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: "#b91c1c",
    borderRadius: 12,
    paddingHorizontal: 14,
    paddingVertical: 10,
    marginBottom: 16,
  },
  hwBannerText: { color: "#fff", fontWeight: "700", marginLeft: 8 },
  status: { fontSize: 16, color: "#374151", marginBottom: 24 },
  recBtn: {
    width: 180,
    height: 180,
    borderRadius: 90,
    backgroundColor: "#111827",
    alignItems: "center",
    justifyContent: "center",
  },
  recBtnActive: { backgroundColor: "#b91c1c" },
  recText: { color: "#fff", fontSize: 22, fontWeight: "800" },
  error: { color: "#b91c1c", marginTop: 16, textAlign: "center" },
  result: { marginTop: 24, alignItems: "stretch", width: "100%", backgroundColor: "#f9fafb", borderRadius: 12, padding: 16 },
  stepRow: { flexDirection: "row", alignItems: "center", paddingVertical: 4 },
  dot: { width: 10, height: 10, borderRadius: 5, backgroundColor: "#d1d5db", marginRight: 10, marginLeft: 5 },
  dotActive: { backgroundColor: "#2563eb" },
  stepText: { color: "#9ca3af" },
  stepTextActive: { color: "#111827", fontWeight: "700" },
  attention: { color: "#92400e", marginTop: 8, textAlign: "center" },
  done: { textAlign: "center", marginVertical: 12, color: "#111827" },
  openBtn: { backgroundColor: "#2563eb", borderRadius: 10, paddingHorizontal: 20, paddingVertical: 12, alignItems: "center" },
  openText: { color: "#fff", fontWeight: "700" },
});
