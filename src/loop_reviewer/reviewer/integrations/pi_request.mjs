// One model exchange inside Pi. No agent conversation, repository tools or automatic follow-up.
import { readFileSync, writeFileSync } from "node:fs";

export default function reviewRequest(pi) {
	pi.on("session_start", async (_event, ctx) => {
		let result = { status: "unsupported_model" };
		try {
			const request = JSON.parse(readFileSync(0, "utf8"));
			const slash = request.model.indexOf("/");
			const model = ctx.modelRegistry.find(
				request.model.slice(0, slash),
				request.model.slice(slash + 1),
			);
			if (!model || !ctx.modelRegistry.hasConfiguredAuth(model)) {
				result.reason = model ? "missing_auth" : "unknown_model";
				return;
			}
			if (
				![
					"openai-completions",
					"openai-responses",
					"openai-codex-responses",
					"anthropic-messages",
				].includes(model.api)
			)
				return;
			const { input, output, cacheRead, cacheWrite } = model.cost;
			if (
				![input, output, cacheRead, cacheWrite].every(
					(n) => Number.isFinite(n) && n >= 0,
				) ||
				input <= 0 ||
				output <= 0
			)
				return;
			const profile = {
				provider: model.provider,
				id: model.id,
				api: model.api,
				endpoint: model.baseUrl,
				inputCap: model.contextWindow,
				outputCap: Math.min(2048, model.maxTokens),
				inputRate: Math.ceil(Math.max(input, cacheRead, cacheWrite) * 1e6),
				outputRate: Math.ceil(output * 1e6),
			};
			if (!request.profile) {
				result = { status: "ready", profile };
				return;
			}
			if (JSON.stringify(profile) !== JSON.stringify(request.profile)) return;
			result = { status: "transient_error" };
			let dispatched = false;
			const reply = await ctx.modelRegistry.complete(
				model,
				{
					messages: [
						{ role: "user", content: request.prompt, timestamp: Date.now() },
					],
				},
				{
					maxTokens: profile.outputCap,
					maxRetries: 0,
					cacheRetention: "none",
					transport: "sse",
					onPayload(payload) {
						// Provider defaults and custom sampling parameters can override maxTokens.
						const cap =
							payload.max_completion_tokens ??
							payload.max_output_tokens ??
							payload.max_tokens;
						if (
							dispatched ||
							payload.model !== model.id ||
							!Number.isSafeInteger(request.inputCap) ||
							Buffer.byteLength(JSON.stringify(payload)) + 256 > request.inputCap ||
							(payload.n ?? 1) !== 1 ||
							(payload.service_tier &&
								!["default", "standard_only"].includes(payload.service_tier))
						)
							throw new Error("Request differs from reserved model or tier");
						dispatched = true;
						if (
							!Number.isSafeInteger(cap) ||
							cap <= 0 ||
							cap > profile.outputCap
						)
							throw new Error("Unbounded provider request");
					},
				},
			);
			const {
				input: fresh,
				output: generated,
				cacheRead: read,
				cacheWrite: written,
				totalTokens,
			} = reply.usage;
			const status =
				{
					stop: "success",
					length: "truncated",
					error: "transient_error",
					aborted: "transient_error",
				}[reply.stopReason] ?? "invalid_output";
			result = {
				status,
				model: reply.provider + "/" + reply.model,
				usage: [fresh + read + written, generated, totalTokens],
				content: reply.content
					.filter((block) => block.type === "text")
					.map((block) => block.text)
					.join(""),
			};
		} finally {
			writeFileSync(1, JSON.stringify(result) + "\n");
			ctx.shutdown();
		}
	});
}
