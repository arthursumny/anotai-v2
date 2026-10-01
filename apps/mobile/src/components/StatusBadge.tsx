import React from "react";
import { StyleSheet, Text, View } from "react-native";

import type { NoteStatus } from "../types";

const LABEL: Record<NoteStatus, string> = {
  processed: "Processada",
  no_match: "Nota geral",
  pending_review: "Em análise",
};

const COLORS: Record<NoteStatus, { bg: string; fg: string }> = {
  processed: { bg: "#dcfce7", fg: "#166534" },
  no_match: { bg: "#ffedd5", fg: "#9a3412" },
  pending_review: { bg: "#e5e7eb", fg: "#374151" },
};

export default function StatusBadge({ status, templateId }: { status: NoteStatus; templateId?: string | null }): JSX.Element {
  const isGeneral = status === "processed" && templateId === null;
  const label = status === "no_match" ? "Nota geral" : isGeneral ? "Nota geral" : LABEL[status];
  const c = isGeneral ? { bg: "#dbeafe", fg: "#1e40af" } : COLORS[status];
  return (
    <View style={[styles.badge, { backgroundColor: c.bg }]}>
      <Text style={[styles.text, { color: c.fg }]}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    alignSelf: "flex-start",
    borderRadius: 999,
    paddingHorizontal: 10,
    paddingVertical: 4,
  },
  text: { fontSize: 12, fontWeight: "700" },
});
