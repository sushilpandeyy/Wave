/**
 * Chat state hook: owns the message list, connection status, and the streaming buffer,
 * and drives a single WsChatClient for the connection's lifetime.
 *
 * Token frames append to a live assistant bubble; `done` finalizes it (and tags mood);
 * `notice` frames render as Wave speaking out-of-band.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { DEV_USER_ID, WS_URL } from '@/config/env';
import type { ChatMessage, ConnStatus, ServerFrame } from '@/domain/types';

import { WsChatClient } from './WsChatClient';

let _seq = 0;
const newId = () => `${Date.now()}-${_seq++}`;

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<ConnStatus>('connecting');
  const clientRef = useRef<WsChatClient | null>(null);
  // Id of the assistant bubble currently being streamed (null between turns).
  const streamingIdRef = useRef<string | null>(null);

  const handleFrame = useCallback((frame: ServerFrame) => {
    if (frame.type === 'token') {
      if (streamingIdRef.current === null) {
        const id = newId();
        streamingIdRef.current = id;
        setMessages((prev) => [
          ...prev,
          { id, role: 'assistant', text: frame.value, streaming: true },
        ]);
      } else {
        const id = streamingIdRef.current;
        setMessages((prev) =>
          prev.map((m) => (m.id === id ? { ...m, text: m.text + frame.value } : m)),
        );
      }
    } else if (frame.type === 'done') {
      const id = streamingIdRef.current;
      streamingIdRef.current = null;
      if (id) {
        setMessages((prev) =>
          prev.map((m) => (m.id === id ? { ...m, streaming: false, mood: frame.mood } : m)),
        );
      }
    } else if (frame.type === 'notice') {
      setMessages((prev) => [...prev, { id: newId(), role: 'notice', text: frame.message }]);
    }
  }, []);

  useEffect(() => {
    const client = new WsChatClient(`${WS_URL}?user_id=${DEV_USER_ID}`, {
      onFrame: handleFrame,
      onStatus: setStatus,
    });
    clientRef.current = client;
    client.connect();
    return () => client.close();
  }, [handleFrame]);

  const send = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    setMessages((prev) => [...prev, { id: newId(), role: 'user', text: trimmed }]);
    const ok = clientRef.current?.send(trimmed);
    if (!ok) {
      setMessages((prev) => [
        ...prev,
        {
          id: newId(),
          role: 'notice',
          text: "I'm just reconnecting — give me a sec and send that again?",
        },
      ]);
    }
  }, []);

  return { messages, status, send };
}
