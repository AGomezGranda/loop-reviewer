// @vitest-environment jsdom

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { openWatch } from "@/api/client";
import { useInboxWatch } from "./use-inbox-watch";

afterEach(() => {
	cleanup();
	vi.useRealTimers();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

test("reconnects after a transient watch failure and stops when the session is rejected", async () => {
	vi.useFakeTimers();
	sessionStorage.setItem("loop-reviewer.capability", "test");
	const fetcher = vi
		.fn(async () =>
			Response.json(
				{
					code: "unauthorized",
					message: "Open a fresh browser session with loop-reviewer ui.",
				},
				{ status: 401 },
			),
		)
		.mockRejectedValueOnce(new TypeError("connection interrupted"));
	vi.stubGlobal("fetch", fetcher);
	const invalidate = vi.fn();
	const { result } = renderHook(() => useInboxWatch(invalidate));
	await act(async () => vi.advanceTimersByTimeAsync(0));
	expect(result.current).toBe("disconnected");
	await act(async () => vi.advanceTimersByTimeAsync(8000));
	expect(result.current).toBe("unauthorized");
	expect(fetcher).toHaveBeenCalledTimes(2);
	expect(invalidate).not.toHaveBeenCalled();
});

test("decodes split inbox events and rejects an unknown event", async () => {
	sessionStorage.setItem("loop-reviewer.capability", "test");
	const body = new ReadableStream({
		start(controller) {
			const encode = new TextEncoder();
			controller.enqueue(encode.encode('{"event":"rea'));
			controller.enqueue(
				encode.encode(
					'dy"}\n{"event":"heartbeat"}\n{"event":"scan-finished"}\n{"event":"unknown"}\n',
				),
			);
			controller.close();
		},
	});
	vi.stubGlobal(
		"fetch",
		vi.fn(async () => new Response(body)),
	);
	const stream = openWatch(new AbortController().signal);

	for (const event of ["ready", "heartbeat", "scan-finished"]) {
		await expect(stream.next()).resolves.toEqual({ done: false, value: event });
	}
	await expect(stream.next()).rejects.toThrow("Invalid inbox watch event");
});
