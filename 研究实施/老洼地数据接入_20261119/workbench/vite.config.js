import {defineConfig} from "vite";

const astryxPackages = /node_modules[\\/](?:@astryxdesign|@stylexjs)[\\/]/;
const vendors = [
  {name: "vendor-react", test: /node_modules[\\/](?:react|react-dom|scheduler)[\\/]/},
  {
    name: "vendor-astryx",
    test: id => astryxPackages.test(id) && !id.endsWith(".css"),
    // Keep components used only by lazy views on demand. Rolldown sizes groups before minification.
    tags: ["$initial"],
    maxSize: 1_900_000,
  },
  {name: "vendor-lucide", test: /node_modules[\\/]lucide-react[\\/]/, tags: ["$initial"]},
];

export default defineConfig({
  build: {
    rolldownOptions: {
      output: {
        // Preserve initialization order when the Astryx vendor group spans multiple chunks.
        strictExecutionOrder: true,
        codeSplitting: {
          includeDependenciesRecursively: false,
          groups: vendors,
        },
      },
    },
  },
});
