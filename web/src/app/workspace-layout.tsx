import { useQueryClient } from "@tanstack/react-query";
import {
	Archive,
	GitPullRequest,
	Inbox,
	ListChecks,
	Settings,
} from "lucide-react";
import { useCallback } from "react";
import { Link, NavLink, Outlet } from "react-router";
import { inboxQuery } from "@/features/inbox/inbox-queries";
import { OpenReviewForm } from "@/features/review/open-review-form";
import { useReviewLaunch } from "./review-navigation";
import { useInboxWatch } from "./use-inbox-watch";

const navigation = [
	{ path: "/inbox", text: "Inbox", Icon: Inbox },
	{ path: "/reviews", text: "Reviews", Icon: ListChecks },
	{ path: "/history", text: "History", Icon: Archive },
	{ path: "/setup", text: "Setup", Icon: Settings },
];

export function ReviewsLink() {
	return (
		<Link className="text-primary underline underline-offset-4" to="/reviews">
			View Reviews
		</Link>
	);
}

export function WorkspaceLayout() {
	const cache = useQueryClient();
	const refreshInbox = useCallback(() => {
		void cache.invalidateQueries({ queryKey: inboxQuery.queryKey });
	}, [cache]);
	const watchStatus = useInboxWatch(refreshInbox);
	const launch = useReviewLaunch();

	return (
		<div className="min-h-svh md:grid md:grid-cols-[12rem_minmax(0,1fr)]">
			<aside className="border-b bg-sidebar px-4 py-3 text-sidebar-foreground md:border-r md:border-b-0 md:px-3 md:py-5">
				<div className="mb-3 flex items-center gap-2 px-1.5 text-sm font-semibold md:mb-6">
					<span className="grid size-8 place-items-center rounded-md border border-primary/30 bg-primary/10 text-primary">
						<GitPullRequest aria-hidden="true" className="size-4" />
					</span>
					Loop Reviewer
				</div>
				<nav
					aria-label="Workspace"
					className="grid grid-cols-4 gap-1 md:grid-cols-1"
				>
					{navigation.map(({ path, text, Icon }) => (
						<NavLink
							key={path}
							to={path}
							className={({ isActive }) =>
								`flex flex-col items-center justify-center gap-1 rounded-md px-2 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring sm:flex-row sm:gap-2 md:justify-start ${isActive ? "bg-sidebar-accent text-sidebar-accent-foreground" : "hover:bg-muted"}`
							}
						>
							<Icon className="size-4" aria-hidden="true" />
							{text}
						</NavLink>
					))}
				</nav>
				{(watchStatus === "disconnected" || watchStatus === "unauthorized") && (
					<p role="status" className="mt-3 px-2 text-xs text-destructive">
						{watchStatus === "unauthorized"
							? "Open a fresh browser session with loop-reviewer ui."
							: "Inbox watch disconnected"}
					</p>
				)}
			</aside>
			<div className="min-w-0">
				<header className="border-b bg-card">
					<div className="mx-auto max-w-5xl px-4 py-4 sm:px-6">
						<OpenReviewForm onAccepted={launch} reviewsLink={<ReviewsLink />} />
					</div>
				</header>
				<main className="mx-auto max-w-5xl px-4 py-5 sm:px-6 sm:py-6">
					<Outlet context={watchStatus} />
				</main>
			</div>
		</div>
	);
}
