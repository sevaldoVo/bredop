import { useEffect, useState } from 'react';
import Home from './components/Home';
import Board from './components/Board';
import { useToast } from './ui';
import { useAuth } from './useAuth';
import HalftoneCanvas from './components/HalftoneCanvas';
import type { AuthUser } from './api';
import type { InvestigationCase } from './types';

/** ЭТАП 7: форма входа/регистрации поверх доски (когда JWT нет в localStorage). */
function AuthGate({ auth }: { auth: ReturnType<typeof useAuth> }) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email || password.length < 6) return;
    const ok = mode === 'login' ? await auth.login(email, password) : await auth.register(email, password);
    if (!ok && auth.error) { /* ошибка уже в auth.error, покажем под формой */ }
  };

  return (
    <div className="auth-gate" role="dialog" aria-modal="true">
      {/* ЭТАП 9: сплит-экран — слева halftone-полотно + лого Unbounded 96px, справа форма */}
      <div className="auth-left">
        <div className="halftone-layer"><HalftoneCanvas /></div>
        <div className="auth-wordmark">ВИЛКА</div>
      </div>
      <div className="auth-right">
      <form className="auth-card" onSubmit={(e) => void submit(e)}>
        <div className="auth-logo">ВИЛКА</div>
        <div className="auth-sub">OSINT-платформа · {mode === 'login' ? 'вход' : 'регистрация'}</div>
        <label>
          Email
          <input
            type="email" autoComplete="email" placeholder="you@vilka.app"
            value={email} onChange={(e) => setEmail(e.target.value)} required
          />
        </label>
        <label>
          Пароль
          <input
            type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            placeholder="мин. 8 символов" minLength={6}
            value={password} onChange={(e) => setPassword(e.target.value)} required
          />
        </label>
        {auth.error && <div className="auth-error">{String(auth.error).slice(0, 120)}</div>}
        <button className="btn primary" type="submit" disabled={auth.isBusy}>
          {auth.isBusy ? '…' : mode === 'login' ? 'Войти' : 'Создать аккаунт (+стартовые кредиты)'}
        </button>
        <button className="btn ghost small" type="button" onClick={() => setMode(mode === 'login' ? 'register' : 'login')}>
          {mode === 'login' ? 'Нет аккаунта? Регистрация' : 'Уже есть аккаунт? Вход'}
        </button>
      </form>
      </div>
    </div>
  );
}

export default function App() {
  const [route, setRoute] = useState<{ name: 'home' } | { name: 'board'; c: InvestigationCase }>({ name: 'home' });
  const toast = useToast();
  // ЭТАП 6: JWT-авторизация — прокидываем юзера и обработчик 402 в Board.
  const auth = useAuth();

  useEffect(() => {
    const onVilka = (e: Event) => {
      const d = (e as CustomEvent).detail ?? {};
      if (d.type === 'unauthorized') auth.logout();
    };
    window.addEventListener('vilka-api', onVilka);
    return () => window.removeEventListener('vilka-api', onVilka);
  }, [auth]);

  const handlePaymentRequired = (u: AuthUser) => {
    toast.show(`Кредиты: ${u.credits_balance}. Пополните баланс.`);
  };

  /** ЭТАП 7: «Пополнить» — создаёт крипто-инвойс и открывает pay_url. */
  const handleTopUp = async () => {
    const res = await auth.checkout(25);
    if (res?.pay_url) {
      window.open(res.pay_url, '_blank', 'noopener');
      toast.show(`Инвойс на ${res.credits} кредитов создан`);
    } else {
      toast.show(auth.error ? String(auth.error).slice(0, 90) : 'Не удалось создать инвойс');
    }
  };

  // ЭТАП 7: нет JWT -> форма входа/регистрации поверх всего приложения.
  const showAuthGate = !auth.isAuthed;

  return (
    <>
      {route.name === 'home' ? (
        <Home
          toast={toast.show}
          onOpenCase={(c) => setRoute({ name: 'board', c })}
        />
      ) : (
        <Board
          key={route.c.id}
          initial={route.c}
          onExit={() => setRoute({ name: 'home' })}
          user={auth.user}
          onPaymentRequired={handlePaymentRequired}
          onTopUp={() => void handleTopUp()}
          onLogout={auth.logout}
        />
      )}
      {showAuthGate && <AuthGate auth={auth} />}
      {toast.node}
    </>
  );
}
