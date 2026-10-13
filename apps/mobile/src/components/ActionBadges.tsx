import React from "react";
import { StyleSheet, Text, View } from "react-native";

const LABELS: Record<string, string> = {
  telegram: "Telegram",
  webhook: "Webhook",
  mcp_tool: "MCP",
  email: "E-mail",
  create_task: "Criar tarefa",
};

export default function ActionBadges({ types }: { types: string[] }): JSX.Element {
  const unique = [...new Set(types)];
  if (unique.length === 0) return <Text style={styles.empty}>Sem ações</Text>;
  return (
    <View style={styles.row}>
      {unique.map((t) => (
        <View key={t} style={styles.badge}>
          <Text style={styles.text}>{LABELS[t] ?? t}</Text>
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 8 },
  badge: {
    backgroundColor: "#eef2ff",
    borderRadius: 999,
    paddingHorizontal: 10,
    paddingVertical: 4,
  },
  text: { color: "#3730a3", fontSize: 12, fontWeight: "700" },
  empty: { color: "#6b7280", fontSize: 12, marginTop: 8 },
});
