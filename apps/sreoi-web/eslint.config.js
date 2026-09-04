import { ryoppippi } from '@ryoppippi/eslint-config';

/**
 * `react: true` is deliberately not set. It requires
 * `@eslint-react/eslint-plugin`, which is not in this workspace's catalog, and
 * adding it pulls a plugin tree into a repository whose pnpm settings block
 * trust downgrades. The TypeScript rules -- which is where the real defects
 * live -- apply either way.
 */
export default ryoppippi({
	typescript: true,
});
