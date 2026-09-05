import type { Locale } from './i18n.ts';
import { useState } from 'react';
import { signIn } from './api.ts';
import { t } from './i18n.ts';

/**
 * Sign-in form.
 *
 * Until this existed the client had no way to authenticate: it told the reader
 * to "sign in through the engine", which worked only because a development
 * engine and a development client both answered on 127.0.0.1 and cookies
 * ignore the port. Deployed, the browser never reaches the engine, so the
 * instruction was not merely awkward -- it was impossible to follow.
 *
 * Nothing here verifies anything. The form exchanges a password for a session
 * cookie through the API, and every decision -- whether the password is right,
 * which organisation the user belongs to, what role the database says they
 * have -- stays where it already lives.
 */
export function statusMessage(locale: Locale, status: number): string {
	// 401 and 429 are different facts and must read differently: one means the
	// password is wrong, the other means it might be right but you have run out
	// of attempts. Collapsing them into "sign-in failed" would have people
	// retyping a correct password into a lockout.
	if (status === 429) {
		return t(locale, 'signin.throttled');
	}
	if (status === 403) {
		return t(locale, 'signin.disabled');
	}
	if (status === 401) {
		return t(locale, 'signin.invalid');
	}
	return t(locale, 'signin.unavailable');
}

export function SignIn({
	locale,
	onSignedIn,
}: {
	locale: Locale;
	onSignedIn: () => void;
}): React.JSX.Element {
	const [email, setEmail] = useState('');
	const [password, setPassword] = useState('');
	const [failure, setFailure] = useState<string | null>(null);
	const [busy, setBusy] = useState(false);

	const submit = (event: React.FormEvent): void => {
		event.preventDefault();
		setBusy(true);
		setFailure(null);
		void signIn(email, password)
			.then((result) => {
				if (result.ok) {
					// The password is not kept in state after it has been used.
					setPassword('');
					onSignedIn();
					return;
				}
				setFailure(statusMessage(locale, result.status));
			})
			.catch(() => {
				setFailure(t(locale, 'signin.unavailable'));
			})
			.finally(() => {
				setBusy(false);
			});
	};

	return (
		<section className="panel signin">
			<h2>{t(locale, 'signin.heading')}</h2>
			<p className="muted">{t(locale, 'signin.subtitle')}</p>
			<form onSubmit={submit}>
				<label htmlFor="signin-email">{t(locale, 'signin.email')}</label>
				<input
					id="signin-email"
					name="email"
					type="email"
					autoComplete="username"
					dir="ltr"
					required
					value={email}
					onChange={event => setEmail(event.target.value)}
				/>
				<label htmlFor="signin-password">{t(locale, 'signin.password')}</label>
				<input
					id="signin-password"
					name="password"
					type="password"
					autoComplete="current-password"
					dir="ltr"
					required
					value={password}
					onChange={event => setPassword(event.target.value)}
				/>
				<button type="submit" disabled={busy}>
					{busy ? t(locale, 'signin.working') : t(locale, 'signin.submit')}
				</button>
			</form>
			{failure !== null && (
				<div className="refused" role="alert">
					{failure}
				</div>
			)}
		</section>
	);
}

if (import.meta.vitest != null) {
	describe('statusMessage', () => {
		it('distinguishes a wrong password from a lockout', () => {
			// Otherwise a throttled user retypes a correct password into a wall.
			expect(statusMessage('en', 401)).not.toBe(statusMessage('en', 429));
		});

		it('says so when password login is disabled rather than blaming the password', () => {
			// 403 is what the engine returns with OIDC configured: the credential
			// is not wrong, this endpoint is simply not how you sign in here.
			expect(statusMessage('en', 403)).toBe(t('en', 'signin.disabled'));
		});

		it('falls back to unavailable for an unexpected status', () => {
			expect(statusMessage('en', 503)).toBe(t('en', 'signin.unavailable'));
		});

		it('translates', () => {
			expect(statusMessage('ar', 401)).not.toBe(statusMessage('en', 401));
		});
	});
}
