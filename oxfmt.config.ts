import { defineConfig } from 'oxfmt';

export default defineConfig({
	useTabs: true,
	singleQuote: true,
	ignorePatterns: [
		'dist',
		'node_modules',
		'coverage',
		'.git',
		'**/pnpm-lock.yaml',
		// `platform/` is a Python project with its own toolchain (ruff for code,
		// prose markdown for docs) and a vendored third-party MapLibre bundle.
		// oxfmt has never formatted it, and three separate things go wrong when
		// it tries: it parses the Jinja templates as HTML and fails outright on
		// the `{% %}` blocks, which blocks every commit in the repository rather
		// than only changes to those files; it would reformat ~30 committed
		// design documents; and it would rewrite vendored code that must stay
		// byte-identical to what upstream published.
		'platform/**',
	],
});
