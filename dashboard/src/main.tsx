import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { createApi } from "./api/client";
import { ApiProvider } from "./api/context";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");

createRoot(root).render(
  <StrictMode>
    <ApiProvider api={createApi()}>
      <App />
    </ApiProvider>
  </StrictMode>,
);
