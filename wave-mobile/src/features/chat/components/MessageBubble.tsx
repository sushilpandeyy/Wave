import { StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import type { ChatMessage } from '@/domain/types';

const ACCENT = '#3c87f7';

export function MessageBubble({ message }: { message: ChatMessage }) {
  const theme = useTheme();

  // Notices are Wave speaking out-of-band — centered and quiet, never an error.
  if (message.role === 'notice') {
    return (
      <View style={styles.noticeRow}>
        <ThemedText type="small" themeColor="textSecondary" style={styles.noticeText}>
          {message.text}
        </ThemedText>
      </View>
    );
  }

  const isUser = message.role === 'user';

  return (
    <View style={[styles.row, isUser ? styles.rowEnd : styles.rowStart]}>
      <View
        style={[
          styles.bubble,
          isUser
            ? { backgroundColor: ACCENT, borderBottomRightRadius: Spacing.one }
            : { backgroundColor: theme.backgroundElement, borderBottomLeftRadius: Spacing.one },
        ]}>
        <ThemedText
          style={[styles.text, isUser && styles.userText]}
          themeColor={isUser ? undefined : 'text'}>
          {message.text}
          {message.streaming ? '▍' : ''}
        </ThemedText>
        {!isUser && message.mood ? (
          <ThemedText type="small" themeColor="textSecondary" style={styles.mood}>
            {message.mood}
          </ThemedText>
        ) : null}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { width: '100%', marginVertical: Spacing.one, flexDirection: 'row' },
  rowStart: { justifyContent: 'flex-start' },
  rowEnd: { justifyContent: 'flex-end' },
  bubble: {
    maxWidth: '82%',
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    borderRadius: Spacing.four,
  },
  text: { fontSize: 16, lineHeight: 22 },
  userText: { color: '#ffffff' },
  mood: { marginTop: Spacing.half, fontStyle: 'italic' },
  noticeRow: { width: '100%', alignItems: 'center', paddingVertical: Spacing.two },
  noticeText: { textAlign: 'center', fontStyle: 'italic', maxWidth: '85%' },
});
