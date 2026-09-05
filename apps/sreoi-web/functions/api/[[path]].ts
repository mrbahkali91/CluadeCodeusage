import type { PagesContext, ProxyEnv } from '../_proxy.ts';
import { proxy } from '../_proxy.ts';

/** Everything under /api/* goes to the origin API. */
export async function onRequest(context: PagesContext<ProxyEnv>): Promise<Response> {
	return proxy(context.request, context.env);
}
