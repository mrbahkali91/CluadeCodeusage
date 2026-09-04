import { defineConfig } from 'vitest/config';

export default defineConfig({
	test: {
		watch: false,
		includeSource: ['src/**/*.{js,ts,tsx}'],
		globals: true,
	},
});
