/** Who is making this request, and on whose behalf. */

export const ROLES = ['VIEWER', 'ANALYST', 'ADMIN', 'ORG_ADMIN', 'PLATFORM_ADMIN'] as const;
export type Role = (typeof ROLES)[number];

/** Rank order, so `atLeast` is a comparison rather than a set of special cases. */
const RANK: Record<Role, number> = {
	VIEWER: 0,
	ANALYST: 1,
	ADMIN: 2,
	ORG_ADMIN: 3,
	PLATFORM_ADMIN: 4,
};

export function isRole(value: string): value is Role {
	return (ROLES as readonly string[]).includes(value);
}

export function atLeast(held: Role, required: Role): boolean {
	return RANK[held] >= RANK[required];
}

export interface Principal {
	subject: string;
	email: string | null;
	organizationId: string;
	organization: string;
	role: Role;
	credential: string;
}

if (import.meta.vitest != null) {
	describe('role ranking', () => {
		it('orders roles so a higher one satisfies a lower requirement', () => {
			expect(atLeast('ORG_ADMIN', 'ANALYST')).toBe(true);
			expect(atLeast('VIEWER', 'ANALYST')).toBe(false);
			expect(atLeast('ANALYST', 'ANALYST')).toBe(true);
		});

		it('rejects a role the platform does not define, so a forged claim cannot invent one', () => {
			expect(isRole('SUPERUSER')).toBe(false);
			expect(isRole('ORG_ADMIN')).toBe(true);
			expect(isRole('org_admin')).toBe(false);
		});

		it('ranks every declared role, so none is silently unorderable', () => {
			for (const role of ROLES) {
				expect(typeof RANK[role]).toBe('number');
			}
		});
	});
}
