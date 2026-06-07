/**
 * Backend connection config.
 *
 * The Android emulator reaches the host machine on 10.0.2.2 (its alias for the host's
 * 127.0.0.1); iOS simulator and web use localhost directly. Override either with
 * EXPO_PUBLIC_WAVE_HOST (e.g. a LAN IP "192.168.1.111:8000" for a physical device).
 */
import { Platform } from 'react-native';

const DEFAULT_HOST = Platform.OS === 'android' ? '10.0.2.2:8000' : 'localhost:8000';

export const WAVE_HOST = process.env.EXPO_PUBLIC_WAVE_HOST ?? DEFAULT_HOST;

export const API_URL = `http://${WAVE_HOST}`;
export const WS_URL = `ws://${WAVE_HOST}/ws/chat`;

/**
 * Until the backend has auth, we chat as a seeded user. Defaults to the free user
 * "Cleo" from `scripts/seed.py`. Override with EXPO_PUBLIC_WAVE_USER_ID.
 */
export const DEV_USER_ID =
  process.env.EXPO_PUBLIC_WAVE_USER_ID ?? '1953f8fb-69e8-46e9-9f6d-8e784dcb4fc2';
