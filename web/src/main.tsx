import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { initializeCapability } from "@/api/session";
import { App } from "@/app/app";
import { createWorkspaceRouter } from "@/app/routes";
import "./index.css";

const container = document.getElementById("root");
if (!container) throw new Error("Missing root element");
const capability = initializeCapability();

createRoot(container).render(
	<StrictMode>
		{capability ? (
			<App router={createWorkspaceRouter()} />
		) : (
			<main className="mx-auto max-w-xl space-y-3 px-5 py-12">
				<h1 className="text-2xl font-semibold">Open Loop Reviewer</h1>
				<p className="text-muted-foreground">
					Start a private browser session from the terminal.
				</p>
				<code className="block rounded-md border bg-card px-3 py-2 text-sm text-primary">
					loop-reviewer ui
				</code>
			</main>
		)}
	</StrictMode>,
);
