import { GlobalRegistrator } from "@happy-dom/global-registrator";

// Each test file runs in its own process; register the DOM once per process.
if (typeof globalThis.document === "undefined") {
  GlobalRegistrator.register({ url: "https://cafe.example/menu" });
}
