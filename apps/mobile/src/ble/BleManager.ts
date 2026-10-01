/**
 * AnotAI BLE — gerenciador de conexão com a lapela (react-native-ble-plx).
 *
 * - Escaneia filtrando pelo ANOTAI_AUDIO_SERVICE_UUID
 * - Persiste o id do dispositivo (pareamento persistente) via SecureStore
 * - Reconexão automática com backoff em queda súbita
 *
 * IMPORTANTE: BLE não funciona no Expo Go — use dev build (`npx expo run:android`).
 */

import { PermissionsAndroid, Platform } from "react-native";
import { BleManager, type Device, type Subscription } from "react-native-ble-plx";
import * as SecureStore from "expo-secure-store";

import {
  ANOTAI_AUDIO_SERVICE_UUID,
  LAPEL_DEVICE_ID_KEY,
  LAPEL_DEVICE_NAME,
} from "./constants";

export type ConnectionStatus =
  | "disconnected"
  | "scanning"
  | "connecting"
  | "connected"
  | "reconnecting";

const MAX_BACKOFF_MS = 30000;

function isLapel(device: Device | null): boolean {
  if (!device) return false;
  const name = device.name ?? device.localName ?? "";
  return name === LAPEL_DEVICE_NAME || name.startsWith(LAPEL_DEVICE_NAME);
}

export class LapelBleManager {
  private ble = new BleManager();
  private status: ConnectionStatus = "disconnected";
  private listeners = new Set<(s: ConnectionStatus) => void>();
  private device: Device | null = null;
  private disconnectSub: Subscription | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempts = 0;
  private wantConnection = false;

  // ---------------------------------------------------------- observadores
  onStatusChange(cb: (s: ConnectionStatus) => void): () => void {
    this.listeners.add(cb);
    cb(this.status);
    return () => {
      this.listeners.delete(cb);
    };
  }

  getStatus(): ConnectionStatus {
    return this.status;
  }

  getDevice(): Device | null {
    return this.device;
  }

  private setStatus(s: ConnectionStatus): void {
    this.status = s;
    for (const cb of this.listeners) cb(s);
  }

  // ------------------------------------------------------------ permissões
  /** Android 12+: BLUETOOTH_SCAN/CONNECT (+ localização p/ scan). iOS: só plist. */
  async requestPermissions(): Promise<boolean> {
    if (Platform.OS !== "android") return true;
    const apiLevel = typeof Platform.Version === "number" ? Platform.Version : 0;
    const perms: string[] = [];
    if (apiLevel >= 31) {
      const P = PermissionsAndroid.PERMISSIONS as Record<string, string>;
      perms.push(P.BLUETOOTH_SCAN, P.BLUETOOTH_CONNECT, P.ACCESS_FINE_LOCATION);
    } else {
      perms.push(PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION);
    }
    const results = await PermissionsAndroid.requestMultiple(perms.filter(Boolean) as never[]);
    return perms.every((p) => results[p as never] === PermissionsAndroid.RESULTS.GRANTED);
  }

  /** Bluetooth do sistema ligado? */
  async isPoweredOn(): Promise<boolean> {
    const state = await this.ble.state();
    return state === "PoweredOn";
  }

  // --------------------------------------------------------------- conexão
  /**
   * Escaneia pela lapela e conecta na primeira encontrada.
   * Retorna o Device conectado. Lança Error em timeout/permissão.
   */
  async scanAndConnect(timeoutMs = 15000): Promise<Device> {
    const ok = await this.requestPermissions();
    if (!ok) throw new Error("Permissão de Bluetooth negada.");
    if (!(await this.isPoweredOn())) throw new Error("Bluetooth desligado.");

    this.wantConnection = true;
    this.setStatus("scanning");

    return new Promise<Device>((resolve, reject) => {
      let done = false;
      const finish = (fn: () => void): void => {
        if (done) return;
        done = true;
        this.ble.stopDeviceScan();
        clearTimeout(timer);
        fn();
      };
      const timer = setTimeout(() => {
        finish(() => {
          this.setStatus("disconnected");
          reject(new Error("Lapela não encontrada. Aproxime-se e tente de novo."));
        });
      }, timeoutMs);

      this.ble.startDeviceScan([ANOTAI_AUDIO_SERVICE_UUID], { allowDuplicates: false }, (error, device) => {
        if (error) {
          finish(() => {
            this.setStatus("disconnected");
            reject(new Error(`Falha no scan BLE: ${error.message}`));
          });
          return;
        }
        if (device && isLapel(device)) {
          finish(() => {
            void this.connectToDevice(device.id).then(resolve, reject);
          });
        }
      });
    });
  }

  async connectToDevice(deviceId: string): Promise<Device> {
    this.wantConnection = true;
    this.clearReconnectTimer();
    this.setStatus("connecting");
    try {
      const device = await this.ble.connectToDevice(deviceId, { autoConnect: false });
      const connected = await device.discoverAllServicesAndCharacteristics();
      this.device = connected;
      this.reconnectAttempts = 0;
      this.watchDisconnection(connected);
      await SecureStore.setItemAsync(LAPEL_DEVICE_ID_KEY, connected.id);
      this.setStatus("connected");
      return connected;
    } catch (e) {
      this.setStatus("disconnected");
      throw new Error(e instanceof Error ? `Falha ao conectar: ${e.message}` : "Falha ao conectar.");
    }
  }

  /** Reconecta ao último dispositivo pareado (silencioso). */
  async reconnectLast(): Promise<Device | null> {
    const id = await SecureStore.getItemAsync(LAPEL_DEVICE_ID_KEY);
    if (!id) return null;
    try {
      return await this.connectToDevice(id);
    } catch {
      return null;
    }
  }

  async forgetDevice(): Promise<void> {
    await this.disconnect();
    await SecureStore.deleteItemAsync(LAPEL_DEVICE_ID_KEY);
  }

  async disconnect(): Promise<void> {
    this.wantConnection = false;
    this.clearReconnectTimer();
    this.disconnectSub?.remove();
    this.disconnectSub = null;
    if (this.device) {
      try {
        await this.ble.cancelDeviceConnection(this.device.id);
      } catch {
        // já desconectado — ok
      }
      this.device = null;
    }
    this.setStatus("disconnected");
  }

  destroy(): void {
    void this.disconnect();
    this.ble.destroy();
  }

  // ---------------------------------------------------------- reconexão
  private watchDisconnection(device: Device): void {
    this.disconnectSub?.remove();
    this.disconnectSub = device.onDisconnected(() => {
      this.device = null;
      if (!this.wantConnection) {
        this.setStatus("disconnected");
        return;
      }
      // Queda súbita: tenta reconectar com backoff, sem derrubar o app.
      this.setStatus("reconnecting");
      this.scheduleReconnect(device.id);
    });
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private scheduleReconnect(deviceId: string): void {
    this.clearReconnectTimer();
    const delay = Math.min(2000 * 2 ** this.reconnectAttempts, MAX_BACKOFF_MS);
    this.reconnectAttempts += 1;
    this.reconnectTimer = setTimeout(() => {
      if (!this.wantConnection) return;
      void this.connectToDevice(deviceId).catch(() => {
        // falhou de novo: mantém "reconnecting" e reagenda
        if (this.wantConnection) {
          this.setStatus("reconnecting");
          this.scheduleReconnect(deviceId);
        }
      });
    }, delay);
  }
}

/** Singleton compartilhado pelo app. */
let shared: LapelBleManager | null = null;
export function getLapelManager(): LapelBleManager {
  if (!shared) shared = new LapelBleManager();
  return shared;
}
