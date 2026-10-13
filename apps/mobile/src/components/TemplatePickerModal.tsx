import React from "react";
import {
  ActivityIndicator,
  FlatList,
  Modal,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";

import type { Template } from "../types";

interface Props {
  visible: boolean;
  templates: Template[];
  loading: boolean;
  busyId: string | null;
  onSelect: (templateId: string) => void;
  onClose: () => void;
}

/** Seletor de template para corrigir manualmente a classificação da nota. */
export default function TemplatePickerModal({
  visible,
  templates,
  loading,
  busyId,
  onSelect,
  onClose,
}: Props): JSX.Element {
  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <SafeAreaView style={styles.container}>
        <View style={styles.header}>
          <Text style={styles.title}>Classificar com um template</Text>
          <Pressable onPress={onClose} style={styles.closeBtn}>
            <Text style={styles.closeText}>Fechar</Text>
          </Pressable>
        </View>
        {loading ? (
          <ActivityIndicator size="large" style={styles.center} />
        ) : templates.length === 0 ? (
          <Text style={styles.empty}>
            Você ainda não tem templates. Clone um no Marketplace para classificar esta nota.
          </Text>
        ) : (
          <FlatList
            data={templates}
            keyExtractor={(t) => t.id}
            renderItem={({ item }) => (
              <Pressable
                style={styles.card}
                onPress={() => onSelect(item.id)}
                disabled={busyId !== null}
              >
                <Text style={styles.cardTitle}>{item.title}</Text>
                <Text style={styles.cardSub} numberOfLines={2}>
                  {item.semantic_trigger}
                </Text>
                {busyId === item.id && <ActivityIndicator style={styles.spin} />}
              </Pressable>
            )}
          />
        )}
      </SafeAreaView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#fff", padding: 16 },
  header: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 12 },
  title: { fontSize: 18, fontWeight: "800" },
  closeBtn: { padding: 8 },
  closeText: { color: "#2563eb", fontWeight: "700" },
  center: { marginTop: 40 },
  empty: { color: "#6b7280", marginTop: 24, textAlign: "center" },
  card: { borderWidth: 1, borderColor: "#e5e7eb", borderRadius: 12, padding: 12, marginBottom: 10 },
  cardTitle: { fontSize: 16, fontWeight: "700" },
  cardSub: { color: "#6b7280", marginTop: 4 },
  spin: { marginTop: 8 },
});
