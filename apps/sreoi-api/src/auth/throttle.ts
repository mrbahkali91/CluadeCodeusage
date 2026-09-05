/**
 * Sign-in throttle.
 *
 * `/auth/login` is the only unauthenticated write path in this service, so it
 * is the one an attacker can hammer. Two things make that worse than it looks:
 * password verification is Argon2, which is deliberately expensive, so
 * unthrottled attempts turn the platform's own defence into a denial-of-service
 * lever; and the engine answers identically for an unknown address and a wrong
 * password, so an attacker learns nothing per attempt and has every reason to
 * make millions of them.
 *
 * Deliberately in-process and in-memory. A shared store (Redis) would survive
 * restarts and cover multiple instances, and when this runs as more than one
 * process it will need one. Reaching for that now would add a dependency, a
 * network hop and a failure mode to a single-process deployment, and an
 * in-process limiter that works is worth more than a distributed one that is
 * not yet deployed. The limitation is real and stated rather than hidden.
 *
 * Failures count, successes clear. A user who mistypes twice and then succeeds
 * is not penalised, and an attacker who never succeeds is slowed on a curve.
 */

/** Attempts allowed in a window before the lockout begins. */
const MAX_FAILURES = 5;
/** Failures older than this are forgotten. */
const WINDOW_MS = 15 * 60 * 1000;
/** How long a key stays locked once it exceeds MAX_FAILURES. */
const LOCKOUT_MS = 15 * 60 * 1000;
/** Above this many tracked keys, the oldest are evicted. */
const MAX_KEYS = 10_000;

interface Bucket {
	failures: number[];
	lockedUntil: number;
}

export interface Decision {
	allowed: boolean;
	retryAfterSeconds: number;
}

export class LoginThrottle {
	private readonly buckets = new Map<string, Bucket>();

	/** `now` is injectable so the tests need no timers. */
	check(key: string, now: number = Date.now()): Decision {
		const bucket = this.buckets.get(key);
		if (bucket === undefined) {
			return { allowed: true, retryAfterSeconds: 0 };
		}
		if (bucket.lockedUntil > now) {
			return {
				allowed: false,
				retryAfterSeconds: Math.ceil((bucket.lockedUntil - now) / 1000),
			};
		}
		return { allowed: true, retryAfterSeconds: 0 };
	}

	recordFailure(key: string, now: number = Date.now()): void {
		const bucket = this.buckets.get(key) ?? { failures: [], lockedUntil: 0 };
		// Prune first: an attempt 20 minutes ago says nothing about now, and
		// keeping it would eventually lock out a legitimate typist.
		bucket.failures = bucket.failures.filter(at => now - at < WINDOW_MS);
		bucket.failures.push(now);
		if (bucket.failures.length >= MAX_FAILURES) {
			bucket.lockedUntil = now + LOCKOUT_MS;
			bucket.failures = [];
		}
		this.buckets.set(key, bucket);
		this.evict();
	}

	recordSuccess(key: string): void {
		this.buckets.delete(key);
	}

	/**
	 * Bound the map so a spray of forged client addresses cannot exhaust memory.
	 *
	 * Insertion order is Map's iteration order, so the first keys out are the
	 * least recently created. An attacker can therefore evict their own lockout
	 * by generating 10,000 distinct addresses -- which requires controlling that
	 * many source addresses, at which point a per-address limiter was never the
	 * control that would stop them. The alternative, an unbounded map, hands the
	 * same attacker the whole process instead.
	 */
	private evict(): void {
		while (this.buckets.size > MAX_KEYS) {
			const oldest = this.buckets.keys().next();
			if (oldest.done === true) {
				return;
			}
			this.buckets.delete(oldest.value);
		}
	}
}

if (import.meta.vitest != null) {
	describe('LoginThrottle', () => {
		const t0 = 1_700_000_000_000;

		it('allows an untried key', () => {
			expect(new LoginThrottle().check('a', t0).allowed).toBe(true);
		});

		it('allows up to the limit and locks on the one after', () => {
			const throttle = new LoginThrottle();
			for (let i = 0; i < 4; i += 1) {
				throttle.recordFailure('a', t0 + i);
			}
			expect(throttle.check('a', t0 + 4).allowed).toBe(true);
			throttle.recordFailure('a', t0 + 4);
			expect(throttle.check('a', t0 + 5).allowed).toBe(false);
		});

		it('reports how long to wait, so the client is not left guessing', () => {
			const throttle = new LoginThrottle();
			for (let i = 0; i < 5; i += 1) {
				throttle.recordFailure('a', t0);
			}
			expect(throttle.check('a', t0).retryAfterSeconds).toBe(15 * 60);
		});

		it('lets the key through again once the lockout expires', () => {
			const throttle = new LoginThrottle();
			for (let i = 0; i < 5; i += 1) {
				throttle.recordFailure('a', t0);
			}
			expect(throttle.check('a', t0 + 15 * 60 * 1000 + 1).allowed).toBe(true);
		});

		it('forgets failures older than the window rather than accumulating them', () => {
			// Four typos today and one next week must not add up to a lockout.
			const throttle = new LoginThrottle();
			for (let i = 0; i < 4; i += 1) {
				throttle.recordFailure('a', t0);
			}
			throttle.recordFailure('a', t0 + 16 * 60 * 1000);
			expect(throttle.check('a', t0 + 16 * 60 * 1000).allowed).toBe(true);
		});

		it('clears the count on a success', () => {
			const throttle = new LoginThrottle();
			for (let i = 0; i < 4; i += 1) {
				throttle.recordFailure('a', t0);
			}
			throttle.recordSuccess('a');
			for (let i = 0; i < 4; i += 1) {
				throttle.recordFailure('a', t0 + 1);
			}
			expect(throttle.check('a', t0 + 1).allowed).toBe(true);
		});

		it('throttles each key independently', () => {
			const throttle = new LoginThrottle();
			for (let i = 0; i < 5; i += 1) {
				throttle.recordFailure('a', t0);
			}
			expect(throttle.check('a', t0).allowed).toBe(false);
			expect(throttle.check('b', t0).allowed).toBe(true);
		});
	});
}
