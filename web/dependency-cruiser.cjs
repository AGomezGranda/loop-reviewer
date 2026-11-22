module.exports = {
	forbidden: [
		{
			name: "no-cycles",
			severity: "error",
			from: {},
			to: { circular: true },
		},
		{
			name: "features-do-not-import-app",
			severity: "error",
			from: { path: "^src/features/" },
			to: { path: "^src/app/" },
		},
		{
			name: "features-coordinate-through-routes",
			severity: "error",
			from: { path: "^src/features/([^/]+)/" },
			to: { path: "^src/features/", pathNot: "^src/features/$1/" },
		},
		{
			name: "shared-code-does-not-import-workflows",
			severity: "error",
			from: { path: "^src/(components|api|lib)/" },
			to: { path: "^src/(features|app)/" },
		},
	],
	options: {
		doNotFollow: { path: "node_modules" },
		tsConfig: { fileName: "tsconfig.json" },
		tsPreCompilationDeps: true,
	},
};
