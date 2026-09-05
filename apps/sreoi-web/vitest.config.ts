import { defineConfig } from 'vitest/config';

export default defineConfig({
	test: {
		watch: false,
		// `functions/` too: the Pages proxy is deployed code, and it decides
		// which headers reach the origin. Untested, that is a security control
		// nobody is checking.
		includeSource: ['src/**/*.{js,ts,tsx}', 'functions/**/*.ts'],
		globals: true,
	},
});
