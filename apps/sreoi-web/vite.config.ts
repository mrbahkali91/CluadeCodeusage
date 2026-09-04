import { defineConfig } from 'vite';

/**
 * No `@vitejs/plugin-react`.
 *
 * That plugin pulls in `@babel/core`, which this workspace's
 * `trustPolicy: no-downgrade` refuses: an earlier published version carried a
 * provenance attestation and the current one does not. Weakening a
 * supply-chain policy to add a build-time convenience is the wrong trade, and
 * Vite's own esbuild transform compiles the automatic JSX runtime without it.
 *
 * What is lost is React Fast Refresh, so editing a component reloads the page
 * instead of preserving state. HMR itself still works.
 */
export default defineConfig({
	esbuild: {
		jsx: 'automatic',
	},
	server: {
		// Loopback, never 0.0.0.0: this dev server talks to an API configured with
		// a development password issuer.
		host: '127.0.0.1',
		port: 5173,
		proxy: {
			// Same-origin in development, so the session cookie the engine sets is
			// sent without any CORS credential dance.
			'/api': { target: 'http://127.0.0.1:3000', changeOrigin: true },
			'/auth': { target: 'http://127.0.0.1:3000', changeOrigin: true },
		},
	},
});
