import { ryoppippi } from '@ryoppippi/eslint-config';

export default ryoppippi(
	{
		typescript: true,
	},
	{
		rules: {
			/**
			 * Off for this app, and it must stay off.
			 *
			 * NestJS resolves dependencies from `design:paramtypes`, the metadata
			 * `emitDecoratorMetadata` writes from constructor parameter types. Those
			 * parameters are the *only* syntactic use of an injected class, so
			 * `consistent-type-imports` sees a type-only import and rewrites it to
			 * `import type` -- which erases the very metadata the container reads.
			 *
			 * This is not hypothetical. Running `lint --fix` converted `Reflector`,
			 * `EngineService`, `DbService`, `MapService` and `OpportunitiesService`
			 * to type imports in one pass, and the application stopped starting.
			 * The failure is also delayed and confusing: typecheck and tests still
			 * pass, because nothing is wrong with the types.
			 */
			'ts/consistent-type-imports': 'off',
		},
	},
);
