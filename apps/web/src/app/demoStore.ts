import { create } from 'zustand';

interface DemoState {
  sessionId: string;
  commandLog: string[];
  setSessionId: (sessionId: string) => void;
  recordCommand: (command: string) => void;
}

export const useDemoStore = create<DemoState>((set) => ({
  sessionId: 'session_demo',
  commandLog: [],
  setSessionId: (sessionId) => set({ sessionId }),
  recordCommand: (command) => set((state) => ({ commandLog: [...state.commandLog.slice(-19), command] })),
}));
