import { fileURLToPath } from "node:url";

const dashboardModules = fileURLToPath(new URL("../../../dashboard/node_modules/", import.meta.url));

export default {
  resolve: {
    alias: [
      { find: "@testing-library/react", replacement: `${dashboardModules}@testing-library/react/dist/index.js` },
      { find: "react/jsx-runtime", replacement: `${dashboardModules}react/jsx-runtime.js` },
      { find: "react/jsx-dev-runtime", replacement: `${dashboardModules}react/jsx-dev-runtime.js` },
      { find: "react-dom/client", replacement: `${dashboardModules}react-dom/client.js` },
      { find: "react-dom", replacement: `${dashboardModules}react-dom/index.js` },
      { find: "react", replacement: `${dashboardModules}react/index.js` },
    ],
  },
  test: { environment: "jsdom", include: ["src/**/*.test.ts", "src/**/*.test.tsx"] },
};
