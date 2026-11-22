import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
	plugins: [react(), tailwindcss()],
	build: {
		outDir: fileURLToPath(
			new URL("../src/loop_reviewer/local_http/dist", import.meta.url),
		),
		emptyOutDir: true,
	},
	resolve: {
		alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
	},
});
