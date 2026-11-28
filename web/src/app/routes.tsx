import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import {
	createBrowserRouter,
	Link,
	redirect,
	useLocation,
	useOutletContext,
	useParams,
} from "react-router";
import { historyKey } from "@/features/history/history-queries";
import {
	CompletionScreen,
	HistoryScreen,
} from "@/features/history/history-screen";
import { inboxQuery } from "@/features/inbox/inbox-queries";
import { InboxScreen } from "@/features/inbox/inbox-screen";
import { RepositoryChecks } from "@/features/inbox/repository-checks";
import { PreviewControl } from "@/features/publication/publication-actions";
import {
	PublicationScreen,
	UnconfirmedPublications,
} from "@/features/publication/publication-screen";
import { draftKey, draftQuery } from "@/features/review/review-queries";
import { reviewsQuery } from "@/features/reviews/reviews-queries";
import { ReviewsScreen } from "@/features/reviews/reviews-screen";
import { SetupScreen } from "@/features/setup/setup-screen";
import { useReviewLaunch, useRouteNavigation } from "./review-navigation";
import { ReviewsLink, WorkspaceLayout } from "./workspace-layout";

const linkStyle =
	"wrap-anywhere text-sm text-primary underline underline-offset-4";

const DraftScreen = lazy(() =>
	import("@/features/review/draft-screen").then(({ DraftScreen }) => ({
		default: DraftScreen,
	})),
);

function InboxRoute() {
	const status = useOutletContext<string>();
	return (
		<InboxScreen
			onAccepted={useReviewLaunch()}
			reviewsLink={<ReviewsLink />}
			disconnected={status === "disconnected"}
			setupLink={
				<Link className={linkStyle} to="/setup">
					Manage repositories
				</Link>
			}
		/>
	);
}

function SetupRoute() {
	const cache = useQueryClient();
	const inbox = useQuery(inboxQuery);
	const report = inbox.data?.kind === "report" ? inbox.data : undefined;
	return (
		<SetupScreen
			checks={report && <RepositoryChecks report={report} />}
			onSaved={() =>
				void cache.invalidateQueries({ queryKey: inboxQuery.queryKey })
			}
		/>
	);
}

function recoveryLink(id: string) {
	return (
		<Link className={linkStyle} to={`/publications/${id}`}>
			Recover publication
		</Link>
	);
}

function completionLink(id: string) {
	return (
		<Link className={linkStyle} to={`/history/${id}`}>
			Open completed review
		</Link>
	);
}

function ReviewsRoute() {
	return (
		<ReviewsScreen
			onRetried={useReviewLaunch()}
			draftLink={(id, notice) => (
				<Link className={linkStyle} to={`/drafts/${id}`} state={{ notice }}>
					Open draft
				</Link>
			)}
			historyLink={completionLink}
			recoveryLink={recoveryLink}
		/>
	);
}

function DraftRoute() {
	const { runId = "" } = useParams();
	const navigate = useRouteNavigation();
	const { state } = useLocation();
	const cache = useQueryClient();
	return (
		<Suspense fallback={<p role="status">Loading draft…</p>}>
			<DraftScreen
				key={runId}
				runId={runId}
				openNotice={
					typeof state?.notice === "string" ? state.notice : undefined
				}
				onFinished={(id) => void navigate(`/history/${id}`)}
				onChanged={() => {
					void cache.invalidateQueries({ queryKey: inboxQuery.queryKey });
					void cache.invalidateQueries({ queryKey: historyKey });
					void cache.invalidateQueries({ queryKey: reviewsQuery.queryKey });
				}}
				historyLink={(id) => (
					<Link className={linkStyle} to={`/history/${id}`}>
						Open completed review
					</Link>
				)}
				recoveryLink={recoveryLink}
				previewControl={(draft, disabled) => (
					<PreviewControl
						runId={draft.runId}
						version={draft.version}
						disabled={disabled}
						warnPartial={
							draft.report?.status === "partial" &&
							draft.feedback.findings.length === 0
						}
						onConflict={() =>
							void cache.invalidateQueries({
								queryKey: draftQuery(runId).queryKey,
							})
						}
						recoveryLink={recoveryLink}
						onPreview={(preview) =>
							void navigate(`/publications/${preview.id}`)
						}
					/>
				)}
			/>
		</Suspense>
	);
}

function HistoryRoute() {
	return (
		<HistoryScreen
			unresolved={
				<UnconfirmedPublications
					publicationLink={(id, title) => (
						<Link className={linkStyle} to={`/publications/${id}`}>
							{title}
						</Link>
					)}
				/>
			}
			completionLink={(id, text) => (
				<Link className={linkStyle} to={`/history/${id}`}>
					{text}
				</Link>
			)}
		/>
	);
}

function CompletionRoute() {
	const { runId = "" } = useParams();
	return (
		<CompletionScreen
			runId={runId}
			backLink={
				<Link className={linkStyle} to="/history">
					Back to history
				</Link>
			}
		/>
	);
}

function PublicationRoute() {
	const { publicationId = "" } = useParams();
	const cache = useQueryClient();
	return (
		<PublicationScreen
			key={publicationId}
			id={publicationId}
			onChanged={() => {
				void cache.invalidateQueries({ queryKey: draftKey });
				void cache.invalidateQueries({ queryKey: historyKey });
				void cache.invalidateQueries({ queryKey: inboxQuery.queryKey });
				void cache.invalidateQueries({ queryKey: reviewsQuery.queryKey });
			}}
			draftLink={(id) => (
				<Link className={linkStyle} to={`/drafts/${id}`}>
					Make a fresh preview
				</Link>
			)}
			historyLink={completionLink}
			recoveryLink={recoveryLink}
		/>
	);
}

export function createWorkspaceRouter() {
	return createBrowserRouter([
		{
			Component: WorkspaceLayout,
			children: [
				{ index: true, loader: () => redirect("/inbox") },
				{ path: "inbox", Component: InboxRoute },
				{ path: "reviews", Component: ReviewsRoute },
				{ path: "setup", Component: SetupRoute },
				{ path: "drafts/:runId", Component: DraftRoute },
				{ path: "history", Component: HistoryRoute },
				{ path: "publications/:publicationId", Component: PublicationRoute },
				{ path: "history/:runId", Component: CompletionRoute },
				{
					path: "*",
					element: (
						<p>
							Page unavailable.{" "}
							<Link className={linkStyle} to="/inbox">
								Open the inbox
							</Link>
						</p>
					),
				},
			],
		},
	]);
}
