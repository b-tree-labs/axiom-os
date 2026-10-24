// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0

import { createRoot } from "react-dom/client";
import { App } from "./App";
// The component styles ship with the dep at build time; the /_appkit
// runtime link stays for tenant token overrides (one canon, ADR-123).
import "@axiom/appkit/style.css";
import "./surface.css";

createRoot(document.getElementById("root")!).render(<App />);
