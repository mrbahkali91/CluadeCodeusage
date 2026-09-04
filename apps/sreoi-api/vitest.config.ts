import { defineConfig } from 'vitest/config';

export default defineConfig({
	test: {
		watch: false,
		// `main.ts` is excluded because it is a bootstrap entry point with nothing
		// to unit test; including it makes vitest fail the file for having no
		// suite. Its `import.meta.vitest` guard stays, so that importing the module
		// under test never starts a listening server.
		includeSource: ['src/**/*.{js,ts}', '!src/main.ts'],
		globals: true,
	},
});
