import type { PagesContext, ProxyEnv } from '../_proxy.ts';
import { proxy } from '../_proxy.ts';

/**
 * Everything under /auth/* goes to the origin API too.
 *
 * A separate route rather than one catch-all at the site root: a root catch-all
 * would swallow every static asset request and proxy the whole site.
 */
export async function onRequest(context: PagesContext<ProxyEnv>): Promise<Response> {
	return proxy(context.request, context.env);
}
