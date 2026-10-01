import { StatusBar } from "expo-status-bar";
import React from "react";

import { AuthProvider } from "./auth/AuthContext";
import RootNavigator from "./navigation/RootNavigator";

export default function App(): JSX.Element {
  return (
    <AuthProvider>
      <StatusBar style="auto" />
      <RootNavigator />
    </AuthProvider>
  );
}
