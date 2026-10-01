import React, { useCallback, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useFocusEffect } from "@react-navigation/native";

import ActionBadges from "../components/ActionBadges";
import { apiErrorMessage, cloneTemplate, listMarketplace } from "../services/api";
import type { Template } from "../types";

export default function MarketplaceScreen(): JSX.Element {
  const [items, setItems] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [cloned, setCloned] = useState<Set<string>>(new Set());
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      setItems(await listMarketplace());
    } catch (e) {
      Alert.alert("Erro", apiErrorMessage(e));
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

  const handleClone = async (id: string, title: string): Promise<void> => {
    setBusyId(id);
    try {
      await cloneTemplate(id);
      setCloned((prev) => new Set(prev).add(id));
      Alert.alert("Pronto", `"${title}" foi clonado para a sua conta.`);
    } catch (e) {
      Alert.alert("Erro", apiErrorMessage(e));
    } finally {
      setBusyId(null);
    }
  };

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" />
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <FlatList
        data={items}
        keyExtractor={(t) => t.id}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => void load(true)} />}
        ListEmptyComponent={<Text style={styles.empty}>Nenhum template público por enquanto.</Text>}
        renderItem={({ item }) => {
          const done = cloned.has(item.id);
          const busy = busyId === item.id;
          return (
            <View style={styles.card}>
              <Text style={styles.title}>{item.title}</Text>
              {item.purpose ? <Text style={styles.desc}>{item.purpose}</Text> : null}
              <Text style={styles.trigger} numberOfLines={3}>
                {item.semantic_trigger}
              </Text>
              <ActionBadges types={item.actions.map((a) => a.type)} />
              <Pressable
                style={[styles.cloneBtn, (done || busy) && styles.cloneBtnDisabled]}
                onPress={() => void handleClone(item.id, item.title)}
                disabled={done || busy}
              >
                {busy ? (
                  <ActivityIndicator color="#fff" />
                ) : (
                  <Text style={styles.cloneText}>{done ? "Clonado" : "Clonar template"}</Text>
                )}
              </Pressable>
            </View>
          );
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#f9fafb", padding: 12 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  empty: { textAlign: "center", color: "#6b7280", marginTop: 40 },
  card: { backgroundColor: "#fff", borderRadius: 12, padding: 14, marginBottom: 10, borderWidth: 1, borderColor: "#e5e7eb" },
  title: { fontSize: 16, fontWeight: "800" },
  desc: { color: "#374151", marginTop: 4 },
  trigger: { color: "#6b7280", fontSize: 12, marginTop: 6, fontStyle: "italic" },
  cloneBtn: { backgroundColor: "#111827", borderRadius: 10, padding: 12, alignItems: "center", marginTop: 12 },
  cloneBtnDisabled: { opacity: 0.5 },
  cloneText: { color: "#fff", fontWeight: "700" },
});
