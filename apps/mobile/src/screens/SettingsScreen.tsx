import React, { useCallback, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { useFocusEffect } from "@react-navigation/native";

import { useAuth } from "../auth/AuthContext";
import {
  apiErrorMessage,
  deleteSecret,
  listSecrets,
  upsertSecret,
} from "../services/api";
import type { Secret } from "../types";

const SUGGESTIONS = ["TELEGRAM_CHAT_ID", "WEBHOOK_AUTH_TOKEN"];

export default function SettingsScreen(): JSX.Element {
  const { user, signOut } = useAuth();
  const [secrets, setSecrets] = useState<Secret[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    try {
      setSecrets(await listSecrets());
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

  const handleSave = async (): Promise<void> => {
    if (!key.trim() || !value) {
      setError("Informe a chave e o valor do segredo.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await upsertSecret(key.trim(), value);
      setKey("");
      setValue("");
      setSecrets(await listSecrets());
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = (secretKey: string): void => {
    Alert.alert("Remover segredo", `Apagar "${secretKey}"? As ações que usam esse segredo vão falhar.`, [
      { text: "Cancelar", style: "cancel" },
      {
        text: "Apagar",
        style: "destructive",
        onPress: () => {
          void (async () => {
            try {
              await deleteSecret(secretKey);
              setSecrets(await listSecrets());
            } catch (e) {
              setError(apiErrorMessage(e));
            }
          })();
        },
      },
    ]);
  };

  const handleSignOut = (): void => {
    Alert.alert("Sair", "Desconectar desta conta?", [
      { text: "Cancelar", style: "cancel" },
      { text: "Sair", style: "destructive", onPress: () => void signOut() },
    ]);
  };

  return (
    <View style={styles.container}>
      <Text style={styles.user}>{user?.name} · {user?.email}</Text>
      {error && <Text style={styles.error}>{error}</Text>}

      <Text style={styles.section}>Novo segredo</Text>
      <Text style={styles.help}>
        Para receber no Telegram: abra o bot oficial e toque em COMEÇAR, descubra seu número com o bot
        @userinfobot (o “Id”, só números) e salve aqui com a chave TELEGRAM_CHAT_ID. Sem token de bot.
      </Text>
      <View style={styles.suggestRow}>
        {SUGGESTIONS.map((s) => (
          <Pressable key={s} style={styles.chip} onPress={() => setKey(s)}>
            <Text style={styles.chipText}>{s}</Text>
          </Pressable>
        ))}
      </View>
      <TextInput
        style={styles.input}
        placeholder="CHAVE (ex: TELEGRAM_CHAT_ID)"
        value={key}
        onChangeText={(t) => setKey(t.toUpperCase())}
        autoCapitalize="characters"
      />
      <TextInput
        style={styles.input}
        placeholder="Valor do segredo"
        value={value}
        onChangeText={setValue}
        secureTextEntry
      />
      <Pressable style={[styles.primary, saving && styles.disabled]} onPress={() => void handleSave()} disabled={saving}>
        {saving ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryText}>Salvar segredo</Text>}
      </Pressable>

      <Text style={styles.section}>Segredos cadastrados</Text>
      {loading ? (
        <ActivityIndicator style={styles.spin} />
      ) : (
        <FlatList
          data={secrets}
          keyExtractor={(s) => s.key}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => void load(true)} />}
          ListEmptyComponent={<Text style={styles.empty}>Nenhum segredo cadastrado.</Text>}
          renderItem={({ item }) => (
            <View style={styles.card}>
              <View style={styles.cardInfo}>
                <Text style={styles.cardKey}>{item.key}</Text>
                <Text style={styles.cardMask}>{item.value_masked}</Text>
              </View>
              <Pressable onPress={() => handleDelete(item.key)} style={styles.delBtn}>
                <Text style={styles.delText}>Apagar</Text>
              </Pressable>
            </View>
          )}
        />
      )}

      <Pressable style={styles.logout} onPress={handleSignOut}>
        <Text style={styles.logoutText}>Deslogar</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#fff", padding: 16 },
  user: { color: "#6b7280", marginBottom: 4 },
  error: { color: "#b91c1c", marginVertical: 8, textAlign: "center" },
  section: { fontSize: 15, fontWeight: "800", marginTop: 16, marginBottom: 8 },
  help: { color: "#6b7280", fontSize: 13, marginBottom: 8 },
  suggestRow: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginBottom: 8 },
  chip: { backgroundColor: "#eef2ff", borderRadius: 999, paddingHorizontal: 10, paddingVertical: 6 },
  chipText: { color: "#3730a3", fontSize: 11, fontWeight: "700" },
  input: { borderWidth: 1, borderColor: "#d1d5db", borderRadius: 10, padding: 12, marginBottom: 10, fontSize: 15 },
  primary: { backgroundColor: "#111827", borderRadius: 10, padding: 13, alignItems: "center" },
  disabled: { opacity: 0.5 },
  primaryText: { color: "#fff", fontWeight: "700" },
  spin: { marginTop: 16 },
  empty: { color: "#6b7280", textAlign: "center", marginTop: 12 },
  card: { flexDirection: "row", alignItems: "center", borderWidth: 1, borderColor: "#e5e7eb", borderRadius: 10, padding: 12, marginBottom: 8 },
  cardInfo: { flex: 1 },
  cardKey: { fontWeight: "800", fontSize: 13 },
  cardMask: { color: "#6b7280", fontFamily: "monospace", marginTop: 2 },
  delBtn: { padding: 8 },
  delText: { color: "#b91c1c", fontWeight: "700" },
  logout: { borderWidth: 1, borderColor: "#fca5a5", borderRadius: 10, padding: 13, alignItems: "center", marginTop: 16, marginBottom: 24 },
  logoutText: { color: "#b91c1c", fontWeight: "700" },
});
