/** Wave chat domain types — mirrors the backend WS frame contract (see app/api.py). */

/** Frames the server sends over the chat socket. */
export type ServerFrame =
  | { type: 'token'; value: string }
  | { type: 'done'; mood?: string | null }
  | { type: 'notice'; message: string };

/** A rendered line in the chat. `notice` is Wave speaking out-of-band (rate limit, etc.). */
export type ChatRole = 'user' | 'assistant' | 'notice';

export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  /** Wave's read of the user's mood, attached on the `done` frame. */
  mood?: string | null;
  /** True while assistant tokens are still streaming in. */
  streaming?: boolean;
}

export type ConnStatus = 'connecting' | 'open' | 'closed';
