import React, { useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { useAuth } from "../auth/AuthContext";
import { apiErrorMessage } from "../services/api";

export default function AuthScreen(): JSX.Element {
  const { signIn, signUp } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") {
        await signIn(email, password);
      } else {
        await signUp(name, email, password);
      }
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const valid =
    email.trim().length > 3 &&
    password.length >= 8 &&
    (mode === "login" || name.trim().length > 0);

  return (
    <View style={styles.container}>
      <Text style={styles.brand}>AnotAI</Text>
      <Text style={styles.sub}>Voz que vira ação</Text>

      {mode === "register" && (
        <TextInput
          style={styles.input}
          placeholder="Nome"
          value={name}
          onChangeText={setName}
          autoCapitalize="words"
        />
      )}
      <TextInput
        style={styles.input}
        placeholder="E-mail"
        value={email}
        onChangeText={setEmail}
        autoCapitalize="none"
        keyboardType="email-address"
      />
      <TextInput
        style={styles.input}
        placeholder="Senha (mín. 8)"
        value={password}
        onChangeText={setPassword}
        secureTextEntry
      />
      {error && <Text style={styles.error}>{error}</Text>}

      <Pressable
        style={[styles.primary, (!valid || busy) && styles.disabled]}
        onPress={submit}
        disabled={!valid || busy}
      >
        {busy ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryText}>{mode === "login" ? "Entrar" : "Criar conta"}</Text>}
      </Pressable>

      <Pressable
        onPress={() => {
          setMode(mode === "login" ? "register" : "login");
          setError(null);
        }}
        style={styles.switch}
      >
        <Text style={styles.switchText}>
          {mode === "login" ? "Não tem conta? Cadastre-se" : "Já tem conta? Entre"}
        </Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, justifyContent: "center", padding: 24, backgroundColor: "#fff" },
  brand: { fontSize: 36, fontWeight: "900", textAlign: "center" },
  sub: { textAlign: "center", color: "#6b7280", marginBottom: 24 },
  input: {
    borderWidth: 1,
    borderColor: "#d1d5db",
    borderRadius: 10,
    padding: 12,
    marginBottom: 12,
    fontSize: 16,
  },
  error: { color: "#b91c1c", marginBottom: 12, textAlign: "center" },
  primary: { backgroundColor: "#111827", borderRadius: 10, padding: 14, alignItems: "center" },
  disabled: { opacity: 0.5 },
  primaryText: { color: "#fff", fontWeight: "700", fontSize: 16 },
  switch: { marginTop: 16, alignItems: "center" },
  switchText: { color: "#2563eb", fontWeight: "600" },
});
