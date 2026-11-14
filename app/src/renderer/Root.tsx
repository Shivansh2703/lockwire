import { StrictMode } from "react";

import { App } from "./App";
import { RootBoundary } from "./ErrorBoundary";

export const Root = () => (
  <StrictMode>
    <RootBoundary>
      <App />
    </RootBoundary>
  </StrictMode>
);
