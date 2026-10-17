import { useEffect, useState } from 'react';
import Home from './components/Home';
import Board from './components/Board';
import { useToast } from './ui';
import { useAuth } from './useAuth';
import type { AuthUser } from './api';
import type { InvestigationCase } from './types';

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
    toast.show(`Кредиты: ${u.credits_balance}. Пополните баланс в личном кабинете.`);
  };

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
        />
      )}
      {toast.node}
    </>
  );
}
