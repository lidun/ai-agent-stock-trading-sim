import { create } from "zustand";
import type { Me } from "../api/endpoints";
import { fetchMe } from "../api/endpoints";

interface AuthState {
  booting: boolean;
  user: Me | null;
  boot: () => Promise<void>;
  setUser: (user: Me | null) => void;
}

export const useAuth = create<AuthState>((set) => ({
  booting: true,
  user: null,
  boot: async () => {
    try {
      const me = await fetchMe();
      set({ user: me, booting: false });
    } catch {
      set({ user: null, booting: false });
    }
  },
  setUser: (user) => set({ user }),
}));
