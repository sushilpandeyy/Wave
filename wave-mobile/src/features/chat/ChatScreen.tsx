import { useEffect, useRef } from 'react';
import { FlatList, KeyboardAvoidingView, Platform, StyleSheet, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ThemedText } from '@/components/themed-text';
import { ThemedView } from '@/components/themed-view';
import { Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import type { ChatMessage, ConnStatus } from '@/domain/types';

import { useChat } from './useChat';
import { Composer } from './components/Composer';
import { MessageBubble } from './components/MessageBubble';

const STATUS_COLOR: Record<ConnStatus, string> = {
  open: '#34c759',
  connecting: '#ff9f0a',
  closed: '#ff3b30',
};

const STATUS_LABEL: Record<ConnStatus, string> = {
  open: 'online',
  connecting: 'connecting…',
  closed: 'reconnecting…',
};

export function ChatScreen() {
  const theme = useTheme();
  const { messages, status, send } = useChat();
  const listRef = useRef<FlatList<ChatMessage>>(null);

  // Keep the latest message in view as the conversation grows / tokens stream in.
  useEffect(() => {
    if (messages.length > 0) {
      requestAnimationFrame(() => listRef.current?.scrollToEnd({ animated: true }));
    }
  }, [messages]);

  return (
    <ThemedView style={styles.fill}>
      <SafeAreaView style={styles.fill} edges={['top', 'bottom']}>
        <View style={[styles.header, { borderBottomColor: theme.backgroundElement }]}>
          <ThemedText type="subtitle" style={styles.title}>
            Wave
          </ThemedText>
          <View style={styles.status}>
            <View style={[styles.dot, { backgroundColor: STATUS_COLOR[status] }]} />
            <ThemedText type="small" themeColor="textSecondary">
              {STATUS_LABEL[status]}
            </ThemedText>
          </View>
        </View>

        <KeyboardAvoidingView
          style={styles.fill}
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <FlatList
            ref={listRef}
            data={messages}
            keyExtractor={(m) => m.id}
            renderItem={({ item }) => <MessageBubble message={item} />}
            contentContainerStyle={styles.listContent}
            ListEmptyComponent={
              <View style={styles.empty}>
                <ThemedText type="small" themeColor="textSecondary" style={styles.emptyText}>
                  Say hi to Wave 👋
                </ThemedText>
              </View>
            }
          />
          <Composer onSend={send} />
        </KeyboardAvoidingView>
      </SafeAreaView>
    </ThemedView>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: Spacing.three,
    paddingBottom: Spacing.two,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  title: { fontSize: 28, lineHeight: 34 },
  status: { flexDirection: 'row', alignItems: 'center', gap: Spacing.two },
  dot: { width: 8, height: 8, borderRadius: 4 },
  listContent: { paddingHorizontal: Spacing.three, paddingVertical: Spacing.three, flexGrow: 1 },
  empty: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  emptyText: { textAlign: 'center' },
});
