import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "@reqpilotx/workbench-ui/styles/index.css";
import "@reqpilotx/workbench-ui/styles/workbench.css";

const root = document.getElementById("root");
if (!root) throw new Error("ReqPilotX root element is missing");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
