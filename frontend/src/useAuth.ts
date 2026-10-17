/**
 * ВИЛКА — ЭТАП 6: React-хук авторизации (JWT в localStorage).
 *
 * Используется вместе с src/api.ts (register/login/checkout оттуда).
 * Токен живёт в localStorage под ключом `vilka_jwt`; api.ts читает его
 * автоматически и подставляет `Authorization: Bearer <token>` на каждый
 * запрос к /api/*, включая WebSocket (query-параметр ?token=...).
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  login as apiLogin,
  register as apiRegister,
  createCheckout,
  fetchMe,
  clearToken,
  loadToken,
  decodeJwtExp,
  type AuthUser,
  type CheckoutResult,
} from './api';

export interface UseAuthResult {
  /** Текущий юзер (или null, если не залогинен / токен протух). */
  user: AuthUser | null;
  isAuthed: boolean;
  isBusy: boolean;
  error: string | null;
  login(email: string, password: string): Promise<boolean>;
  register(email: string, password: string): Promise<boolean>;
  logout(): void;
  /** Купить кредиты: создаёт крипто-инвойс, возвращает URL оплаты. */
  checkout(amountUsd?: number): Promise<CheckoutResult | null>;
  /** Принудительно обновить баланс/профиль с сервера. */
  refresh(): Promise<void>;
}

const STORAGE_KEY = 'vilka_jwt';

export function useAuth(): UseAuthResult {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isBusy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** Проверка локального токена + подтягивание профиля. */
  const hydrate = useCallback(async (): Promise<void> => {
    const token = loadToken();
    if (!token) {
      setUser(null);
      return;
    }
    // Клиентская проверка exp — чтобы не дёргать сервер заведомо мёртвым токеном.
    const exp = decodeJwtExp(token);
    if (exp !== null && exp * 1000 <= Date.now()) {
      clearToken();
      setUser(null);
      return;
    }
    try {
      const me = await fetchMe();
      setUser(me);
    } catch {
      // 401 → чистим, остальное оставляем как есть (сервер может лежать)
      clearToken();
      setUser(null);
    }
  }, []);

  useEffect(() => {
    void hydrate();
  }, [hydrate]);

  const login = useCallback(
    async (email: string, password: string): Promise<boolean> => {
      setBusy(true);
      setError(null);
      try {
        const res = await apiLogin(email, password);
        localStorage.setItem(STORAGE_KEY, res.access_token);
        setUser(await fetchMe());
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : 'login failed');
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const register = useCallback(
    async (email: string, password: string): Promise<boolean> => {
      setBusy(true);
      setError(null);
      try {
        const res = await apiRegister(email, password);
        localStorage.setItem(STORAGE_KEY, res.access_token);
        setUser(await fetchMe());
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : 'register failed');
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const logout = useCallback((): void => {
    clearToken();
    setUser(null);
  }, []);

  const checkout = useCallback(
    async (amountUsd = 25): Promise<CheckoutResult | null> => {
      setBusy(true);
      setError(null);
      try {
        const res = await createCheckout(amountUsd);
        // после оплаты баланс поменяется — обновим профиль
        setUser(await fetchMe());
        return res;
      } catch (e) {
        setError(e instanceof Error ? e.message : 'checkout failed');
        return null;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const refresh = useCallback(async (): Promise<void> => {
    await hydrate();
  }, [hydrate]);

  return useMemo(
    () => ({
      user,
      isAuthed: user !== null,
      isBusy,
      error,
      login,
      register,
      logout,
      checkout,
      refresh,
    }),
    [user, isBusy, error, login, register, logout, checkout, refresh],
  );
}
