import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./auth";
import { initTheme } from "./components/ui";
import { installReplay, isDemo } from "./demo/replay";
import "./styles.css";

initTheme();

async function start() {
  // The static demo (GitHub Pages) answers the API from recorded responses; see demo/replay.ts.
  if (isDemo) await installReplay();
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <BrowserRouter basename={import.meta.env.BASE_URL.replace(/\/$/, "")}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </StrictMode>,
  );
}

start();
