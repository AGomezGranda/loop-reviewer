import type { components, paths } from "./schema";
import { getCapability } from "./session";

type Method = "get" | "post" | "patch";
type Route<M extends Method> = {
	[P in keyof paths]: paths[P][M] extends undefined ? never : P;
}[keyof paths];

type Operation<M extends Method, P extends Route<M>> = NonNullable<paths[P][M]>;
type Body<O> = O extends {
	requestBody: { content: { "application/json": infer B } };
}
	? B
	: never;
type Result<O> = O extends {
	responses: { 200: { content: { "application/json": infer R } } };
}
	? R
	: never;
type PathParams<O> = O extends { parameters: { path: infer P } } ? P : never;
type QueryParams<O> = O extends { parameters: { query?: infer Q } }
	? Exclude<Q, undefined>
	: never;
type Options<O> = { signal?: AbortSignal; query?: QueryParams<O> } & ([
	PathParams<O>,
] extends [never]
	? { params?: never }
	: { params: PathParams<O> }) &
	([Body<O>] extends [never] ? { body?: never } : { body: Body<O> });

export class ApiError extends Error {
	constructor(readonly details: components["schemas"]["ErrorResponse"]) {
		super(details.message);
		this.name = "ApiError";
	}
}

export function errorMessage(error: unknown): string {
	return error instanceof ApiError
		? error.message
		: "The local workspace could not be reached. Check that loop-reviewer ui is still running.";
}

async function authenticatedFetch(url: string, init: RequestInit = {}) {
	const capability = getCapability();
	if (!capability) {
		throw new ApiError({
			code: "unauthorized",
			message: "Run loop-reviewer ui to open a new browser session.",
		});
	}
	const headers = new Headers(init.headers);
	headers.set("Authorization", `Bearer ${capability}`);
	const response = await fetch(url, {
		...init,
		headers,
		cache: "no-store",
		redirect: "error",
	});
	if (!response.ok) {
		const error: components["schemas"]["ErrorResponse"] = await response.json();
		throw new ApiError(error);
	}
	return response;
}

export async function request<M extends Method, P extends Route<M>>(
	method: M,
	path: P,
	options: Options<Operation<M, P>>,
): Promise<Result<Operation<M, P>>> {
	let url: string = path;
	for (const [key, value] of Object.entries(options.params ?? {})) {
		url = url.replace(`{${key}}`, encodeURIComponent(String(value)));
	}
	const query = new URLSearchParams();
	for (const [key, value] of Object.entries(options.query ?? {})) {
		if (value !== undefined) query.set(key, String(value));
	}
	const response = await authenticatedFetch(
		query.size ? `${url}?${query}` : url,
		{
			method: method.toUpperCase(),
			signal: options.signal,
			...(options.body === undefined
				? {}
				: {
						headers: { "Content-Type": "application/json" },
						body: JSON.stringify(options.body),
					}),
		},
	);
	return response.json();
}

const watchPath = "/api/v1/inbox/watch" satisfies Route<"get">;
type WatchEvent =
	paths[typeof watchPath]["get"]["responses"][200]["content"]["application/x-ndjson"]["event"];
const watchEvents = Object.values({
	ready: "ready",
	"scan-finished": "scan-finished",
	heartbeat: "heartbeat",
} as const satisfies { [E in WatchEvent]: E });

export async function* openWatch(
	signal: AbortSignal,
): AsyncGenerator<WatchEvent> {
	const response = await authenticatedFetch(watchPath, { signal });
	if (!response.body) throw new Error("Missing watch stream");

	let pending = "";
	for await (const chunk of response.body.pipeThrough(
		new TextDecoderStream(),
	)) {
		pending += chunk;
		const lines = pending.split("\n");
		pending = lines.pop() ?? "";
		for (const line of lines) {
			if (!line) continue;
			const payload: unknown = JSON.parse(line);
			const event =
				payload && typeof payload === "object" && "event" in payload
					? watchEvents.find((name) => name === payload.event)
					: undefined;
			if (!event) throw new Error("Invalid inbox watch event");
			yield event;
		}
	}
	if (pending) throw new Error("Incomplete inbox watch event");
}
