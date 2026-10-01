import type { NativeStackScreenProps } from "@react-navigation/native-stack";
import React, { useCallback, useState } from "react";
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useFocusEffect } from "@react-navigation/native";

import StatusBadge from "../components/StatusBadge";
import type { NotesStackParamList } from "../navigation/RootNavigator";
import { apiErrorMessage, listNotes } from "../services/api";
import type { Note } from "../types";

type Props = NativeStackScreenProps<NotesStackParamList, "NotesList">;

function preview(n: Note): string {
  const base = n.title?.trim() || n.raw_transcription || "";
  if (!base) return "Áudio em processamento…";
  return base.length > 120 ? `${base.slice(0, 120)}…` : base;
}

export default function NotesListScreen({ navigation }: Props): JSX.Element {
  const [notes, setNotes] = useState<Note[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    try {
      setNotes(await listNotes());
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" />
      </View>
    );
  }

  return (
    <View style={styles.container}>
      {error && <Text style={styles.error}>{error}</Text>}
      <FlatList
        data={notes}
        keyExtractor={(n) => n.id}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => void load(true)} />}
        ListEmptyComponent={<Text style={styles.empty}>Nenhuma nota ainda. Grave seu primeiro áudio!</Text>}
        renderItem={({ item }) => (
          <Pressable style={styles.card} onPress={() => navigation.navigate("NoteDetail", { noteId: item.id })}>
            <StatusBadge status={item.status} templateId={item.template_id} />
            <Text style={styles.date}>{new Date(item.created_at).toLocaleString()}</Text>
            <Text style={styles.preview}>{preview(item)}</Text>
          </Pressable>
        )}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#f9fafb", padding: 12 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  error: { color: "#b91c1c", marginBottom: 8, textAlign: "center" },
  empty: { textAlign: "center", color: "#6b7280", marginTop: 40 },
  card: { backgroundColor: "#fff", borderRadius: 12, padding: 14, marginBottom: 10, borderWidth: 1, borderColor: "#e5e7eb" },
  date: { color: "#6b7280", fontSize: 12, marginTop: 6 },
  preview: { marginTop: 6, fontSize: 14, color: "#111827" },
});
