/** AnotAI mobile — navegação: Auth | Tabs (Gravar, Notas→Stack, Marketplace, Configurações). */

import type { NavigatorScreenParams } from "@react-navigation/native";
import { NavigationContainer } from "@react-navigation/native";
import { createBottomTabNavigator } from "@react-navigation/bottom-tabs";
import { createNativeStackNavigator } from "@react-navigation/native-stack";
import React from "react";
import { ActivityIndicator, View } from "react-native";

import { useAuth } from "../auth/AuthContext";
import AuthScreen from "../screens/AuthScreen";
import MarketplaceScreen from "../screens/MarketplaceScreen";
import NoteDetailScreen from "../screens/NoteDetailScreen";
import NotesListScreen from "../screens/NotesListScreen";
import RecordScreen from "../screens/RecordScreen";
import SettingsScreen from "../screens/SettingsScreen";

export type NotesStackParamList = {
  NotesList: undefined;
  NoteDetail: { noteId: string };
};

export type TabsParamList = {
  Record: undefined;
  Notes: NavigatorScreenParams<NotesStackParamList>;
  Marketplace: undefined;
  Settings: undefined;
};

export type RootStackParamList = {
  Auth: undefined;
  Main: undefined;
};

const Tab = createBottomTabNavigator<TabsParamList>();
const NotesStack = createNativeStackNavigator<NotesStackParamList>();
const RootStack = createNativeStackNavigator<RootStackParamList>();

function NotesNavigator(): JSX.Element {
  return (
    <NotesStack.Navigator>
      <NotesStack.Screen name="NotesList" component={NotesListScreen} options={{ title: "Minhas Notas" }} />
      <NotesStack.Screen name="NoteDetail" component={NoteDetailScreen} options={{ title: "Detalhes da Nota" }} />
    </NotesStack.Navigator>
  );
}

function MainTabs(): JSX.Element {
  return (
    <Tab.Navigator screenOptions={{ headerShown: true }}>
      <Tab.Screen name="Record" component={RecordScreen} options={{ title: "Gravar" }} />
      <Tab.Screen name="Notes" component={NotesNavigator} options={{ title: "Notas", headerShown: false }} />
      <Tab.Screen name="Marketplace" component={MarketplaceScreen} options={{ title: "Marketplace" }} />
      <Tab.Screen name="Settings" component={SettingsScreen} options={{ title: "Configurações" }} />
    </Tab.Navigator>
  );
}

export default function RootNavigator(): JSX.Element {
  const { user, initializing } = useAuth();

  if (initializing) {
    return (
      <View style={{ flex: 1, alignItems: "center", justifyContent: "center" }}>
        <ActivityIndicator size="large" />
      </View>
    );
  }

  return (
    <NavigationContainer>
      <RootStack.Navigator screenOptions={{ headerShown: false }}>
        {user ? (
          <RootStack.Screen name="Main" component={MainTabs} />
        ) : (
          <RootStack.Screen name="Auth" component={AuthScreen} />
        )}
      </RootStack.Navigator>
    </NavigationContainer>
  );
}
